from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, Mapping

import pytest
from blrec_dashboard_api import normalized_repository as normalized_repository_module
from blrec_dashboard_api import service as service_module
from blrec_dashboard_api.app import create_app
from blrec_dashboard_api.database import connect_database, initialize_database
from blrec_dashboard_api.models import IngestBatch
from blrec_dashboard_api.normalized_repository import NormalizedDashboardRepository
from blrec_dashboard_api.service import apply_ingest_batch
from blrec_dashboard_api.settings import ApiSettings
from blrec_dashboard_publisher.cache_sync import sync_dashboard_cache_once
from fastapi.testclient import TestClient

TOKEN = 'incremental-cache-token'


class _StaticRepository:
    def refresh(self, *, force: bool = False) -> bool:
        return False

    def dashboard_payload(self, *, owner_view: bool = False) -> tuple[bytes, str]:
        return b'{}', '0'


def _team(role: str) -> Dict[str, Any]:
    return {
        'role': role,
        'side': 'left' if role == 'ally' else 'right',
        'color': 'teal' if role == 'ally' else 'orange',
        'kills': 10 if role == 'ally' else 8,
        'economy': 40500 if role == 'ally' else 33000,
        'players': [
            {
                'slot': 1,
                'name': '主播' if role == 'ally' else '对手',
                'heroName': '剑圣' if role == 'ally' else '猫女',
                'kills': 5,
                'deaths': 2,
                'assists': 7,
                'economy': 13500,
                'lastHits': 100,
                'isRecordedPlayer': role == 'ally',
            }
        ],
    }


def cache_batch(
    *,
    source_revision: int = 7,
    public_visible: bool = True,
    replay_access: str = 'owner',
) -> Dict[str, Any]:
    return {
        'schemaVersion': 2,
        'sourceRevision': source_revision,
        'publish': True,
        'generatedAt': '2026-08-20T00:00:00Z',
        'sourceLastMatchId': 1,
        'players': [
            {
                'id': 7,
                'name': '主播',
                'initial': '主',
                'roomLabel': '直播间 123456',
                'roomIds': [123456],
                'liveRooms': [],
                'aliases': ['Anchor'],
                'avatarUrl': None,
                'publicVisible': public_visible,
            }
        ],
        'matches': [
            {
                'id': 1,
                'playerId': 7,
                'seasonKey': '2026-summer',
                'mode': '3v3',
                'playedAt': '2026-08-20T00:00:00Z',
                'durationSeconds': 900,
                'result': 'W',
                'streamTitle': '深夜排位',
                'analysisProvisional': False,
                'statsEligible': True,
                'duplicateOfMatchId': None,
                'duplicateReviewState': 'none',
                'ally': _team('ally'),
                'enemy': _team('enemy'),
                'replay': {
                    'kind': 'match',
                    'url': 'https://www.bilibili.com/video/BV1test00001?t=10',
                },
                'replayAccess': replay_access,
            }
        ],
        'removedMatchIds': [],
    }


def test_cache_batch_model_preserves_visibility_dedup_and_revision() -> None:
    batch = IngestBatch.parse_obj(cache_batch())

    assert batch.schema_version == 2
    assert batch.source_revision == 7
    assert batch.publish is True
    assert batch.players[0].public_visible is True
    assert batch.matches[0].stats_eligible is True
    assert batch.matches[0].replay_access == 'owner'
    assert batch.matches[0].duplicate_of_match_id is None
    assert batch.matches[0].duplicate_review_state == 'none'


def test_schema_ten_adds_incremental_visibility_and_audience_heads(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    connection = connect_database(database_path)
    try:
        version = int(
            connection.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[
                0
            ]
        )
        player_columns = {
            str(row['name'])
            for row in connection.execute('PRAGMA table_info(players)').fetchall()
        }
        match_columns = {
            str(row['name'])
            for row in connection.execute('PRAGMA table_info(matches)').fetchall()
        }
        state = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='dashboard_audience_state'"
        ).fetchone()
    finally:
        connection.close()

    assert version == 10
    assert 'public_visible' in player_columns
    assert {
        'stats_eligible',
        'replay_access',
        'duplicate_of_match_id',
        'duplicate_review_state',
    }.issubset(match_columns)
    assert state is not None


def test_incremental_ingest_publishes_both_audiences_and_current_semantics(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    batch = IngestBatch.parse_obj(
        cache_batch(public_visible=False, replay_access='owner')
    )

    result = apply_ingest_batch(database_path, idempotency_key='cache-7', batch=batch)
    connection = connect_database(database_path)
    try:
        player = connection.execute(
            'SELECT public_visible FROM players WHERE player_id=7'
        ).fetchone()
        match = connection.execute(
            'SELECT stats_eligible,replay_access,duplicate_of_match_id,'
            'duplicate_review_state FROM matches WHERE source_match_id=1'
        ).fetchone()
        states = connection.execute(
            'SELECT audience,source_revision FROM dashboard_audience_state '
            'ORDER BY audience'
        ).fetchall()
    finally:
        connection.close()

    assert result['status'] == 'applied'
    assert player is not None and int(player['public_visible']) == 0
    assert match is not None
    assert tuple(match) == (1, 'owner', None, 'none')
    assert [tuple(row) for row in states] == [('owner', 7), ('public', 7)]


def test_normalized_repository_keeps_private_players_and_replays_owner_only(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    apply_ingest_batch(
        database_path,
        idempotency_key='cache-7',
        batch=IngestBatch.parse_obj(
            cache_batch(public_visible=False, replay_access='owner')
        ),
    )
    repository = NormalizedDashboardRepository(
        source_target=tmp_path / 'unused.sqlite3',
        auxiliary_target=database_path,
        revision_loader=lambda _target: 7,
    )

    assert repository.refresh(force=True) is True
    public_dashboard, public_revision = repository.dashboard_document()
    owner_dashboard, owner_revision = repository.dashboard_document(owner_view=True)
    public = repository.list_matches(
        page=1,
        page_size=10,
        season=None,
        mode=None,
        player_id=None,
        query='',
        heroes=(),
        rating_scope='all',
        rating_season=None,
    )
    owner = repository.list_matches(
        page=1,
        page_size=10,
        season=None,
        mode=None,
        player_id=None,
        query='',
        heroes=(),
        rating_scope='all',
        rating_season=None,
        owner_view=True,
    )

    assert public_revision == owner_revision == '7'
    assert public_dashboard['snapshot']['sourceMatchCount'] == 0
    assert owner_dashboard['snapshot']['sourceMatchCount'] == 1
    assert public['total'] == 0
    assert owner['total'] == 1
    assert owner['items'][0]['replayStatus'] == 'available'
    assert owner['items'][0]['replay']['kind'] == 'match'


def test_normalized_repository_skips_payload_reload_when_head_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    apply_ingest_batch(
        database_path,
        idempotency_key='cache-7',
        batch=IngestBatch.parse_obj(cache_batch(source_revision=7)),
    )
    repository = NormalizedDashboardRepository(
        source_target=tmp_path / 'unused.sqlite3', auxiliary_target=database_path
    )
    repository.refresh(force=True)

    def unexpected_payload_reload(_target: object) -> object:
        raise AssertionError('unchanged dashboard head reloaded audience payloads')

    monkeypatch.setattr(
        normalized_repository_module, '_load_state', unexpected_payload_reload
    )

    assert repository.refresh() is False


def test_incremental_endpoint_authenticates_and_refreshes_the_active_repository(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    apply_ingest_batch(
        database_path,
        idempotency_key='cache-7',
        batch=IngestBatch.parse_obj(cache_batch(source_revision=7)),
    )
    settings = ApiSettings(
        database_path=database_path,
        source_database_path=tmp_path / 'unused.sqlite3',
        ingest_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        cors_origins=('https://vg.luwei.host',),
        repository_mode='incremental',
    )

    with TestClient(create_app(settings)) as client:
        unauthorized = client.post(
            '/v1/cache/batches',
            headers={'X-Idempotency-Key': 'cache-8'},
            json=cache_batch(source_revision=8),
        )
        applied = client.post(
            '/v1/cache/batches',
            headers={
                'Authorization': 'Bearer {}'.format(TOKEN),
                'X-Idempotency-Key': 'cache-8',
            },
            json=cache_batch(source_revision=8),
        )
        dashboard = client.get('/v1/dashboard')

    assert unauthorized.status_code == 401
    assert applied.status_code == 200
    assert applied.json()['published'] is True
    assert dashboard.status_code == 200
    assert dashboard.headers['etag'] == 'W/"8"'


def test_direct_repository_rejects_incremental_cache_batches(tmp_path: Path) -> None:
    settings = ApiSettings(
        database_path=tmp_path / 'dashboard.sqlite3',
        source_database_path=tmp_path / 'source.sqlite3',
        ingest_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        cors_origins=('https://vg.luwei.host',),
        repository_mode='direct',
    )

    with TestClient(
        create_app(settings, repository=_StaticRepository())  # type: ignore[arg-type]
    ) as client:
        response = client.post(
            '/v1/cache/batches',
            headers={
                'Authorization': 'Bearer {}'.format(TOKEN),
                'X-Idempotency-Key': 'cache-disabled',
            },
            json=cache_batch(source_revision=8),
        )

    assert response.status_code == 409
    assert response.json()['detail'] == 'dashboard cache ingestion is disabled'


def test_unchanged_source_content_fast_forwards_without_materializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    apply_ingest_batch(
        database_path,
        idempotency_key='cache-7',
        batch=IngestBatch.parse_obj(cache_batch(source_revision=7)),
    )

    def unexpected_materialization(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError('unchanged content must not rebuild dashboard payloads')

    monkeypatch.setattr(
        service_module, 'refresh_dashboard_state', unexpected_materialization
    )
    result = apply_ingest_batch(
        database_path,
        idempotency_key='cache-8',
        batch=IngestBatch.parse_obj(cache_batch(source_revision=8)),
    )
    connection = connect_database(database_path)
    try:
        revisions = {
            int(row['source_revision'])
            for row in connection.execute(
                'SELECT source_revision FROM dashboard_audience_state'
            ).fetchall()
        }
    finally:
        connection.close()

    assert result['published'] is True
    assert revisions == {8}


def test_bootstrap_reset_replaces_stale_rows_without_deleting_assets(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    stale = cache_batch(source_revision=7)
    stale['publish'] = False
    apply_ingest_batch(
        database_path,
        idempotency_key='stale-cache-7',
        batch=IngestBatch.parse_obj(stale),
    )
    connection = connect_database(database_path)
    try:
        connection.execute(
            'INSERT INTO match_assets('
            'source_match_id,image_url,image_width,image_height,image_sha256,updated_at'
            ') VALUES(?,?,?,?,?,?)',
            (1, 'https://example.com/1.webp', 100, 50, 'a' * 64, 1),
        )
        connection.commit()
    finally:
        connection.close()

    fresh = cache_batch(source_revision=8)
    fresh['reset'] = True
    fresh['sourceLastMatchId'] = 2
    fresh['matches'][0]['id'] = 2
    apply_ingest_batch(
        database_path,
        idempotency_key='fresh-cache-8',
        batch=IngestBatch.parse_obj(fresh),
    )
    connection = connect_database(database_path)
    try:
        matches = connection.execute(
            'SELECT source_match_id FROM matches ORDER BY source_match_id'
        ).fetchall()
        assets = connection.execute(
            'SELECT source_match_id FROM match_assets ORDER BY source_match_id'
        ).fetchall()
        batches = connection.execute(
            'SELECT idempotency_key FROM ingestion_batches ORDER BY idempotency_key'
        ).fetchall()
    finally:
        connection.close()

    assert [int(row[0]) for row in matches] == [2]
    assert [int(row[0]) for row in assets] == [1]
    assert [str(row[0]) for row in batches] == ['fresh-cache-8']


def test_bootstrap_defers_rating_rebuild_until_the_published_chunk(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    first = cache_batch(source_revision=8)
    first['publish'] = False
    first['reset'] = True
    first_result = apply_ingest_batch(
        database_path,
        idempotency_key='bootstrap-first',
        batch=IngestBatch.parse_obj(first),
    )
    connection = connect_database(database_path)
    try:
        first_rating_count = int(
            connection.execute('SELECT COUNT(*) FROM rating_events').fetchone()[0]
        )
    finally:
        connection.close()

    final = cache_batch(source_revision=8)
    final['matches'][0]['id'] = 2
    final['sourceLastMatchId'] = 2
    final_result = apply_ingest_batch(
        database_path,
        idempotency_key='bootstrap-final',
        batch=IngestBatch.parse_obj(final),
    )
    connection = connect_database(database_path)
    try:
        rated_match_ids = {
            int(row[0])
            for row in connection.execute(
                'SELECT DISTINCT match_id FROM rating_events'
            ).fetchall()
        }
    finally:
        connection.close()

    assert first_result['ratingEventCount'] == 0
    assert first_rating_count == 0
    assert final_result['ratingEventCount'] > 0
    assert rated_match_ids == {1, 2}


def test_rating_timeline_batches_event_inserts() -> None:
    class Connection:
        def __init__(self) -> None:
            self.rows: list[tuple[object, ...]] = []

        def execute(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError('rating timeline used per-event database writes')

        def executemany(self, sql: str, rows: Iterable[tuple[object, ...]]) -> None:
            assert sql.startswith('INSERT INTO rating_events(')
            self.rows = list(rows)

    connection = Connection()
    final_ability, final_evidence = service_module._insert_rating_timeline(
        connection,
        [
            {'source_match_id': 1, 'player_id': 7, 'result': 'W'},
            {'source_match_id': 2, 'player_id': 7, 'result': 'L'},
        ],
        scope='all',
        season_key='2026-summer',
        previous_ability=None,
        previous_evidence=None,
        reset_visible_score=True,
    )

    assert len(connection.rows) == 2
    assert [int(row[0]) for row in connection.rows] == [1, 2]
    assert all(int(row[-1]) == 7 for row in connection.rows)
    assert final_ability is not None
    assert final_evidence is not None


def test_incremental_search_rebuild_uses_bounded_database_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    source = cache_batch(source_revision=8)
    second = json.loads(json.dumps(source['matches'][0]))
    second['id'] = 2
    second['playedAt'] = '2026-08-20T01:00:00Z'
    source['matches'].append(second)
    source['sourceLastMatchId'] = 2
    apply_ingest_batch(
        database_path,
        idempotency_key='search-bootstrap',
        batch=IngestBatch.parse_obj(source),
    )

    raw_connection = connect_database(database_path)

    class CountingConnection:
        dialect = 'sqlite'

        def __init__(self) -> None:
            self.execute_count = 0
            self.executemany_count = 0

        def execute(
            self, sql: str, parameters: Iterable[object] = ()
        ) -> sqlite3.Cursor:
            self.execute_count += 1
            return raw_connection.execute(sql, tuple(parameters))

        def executemany(
            self, sql: str, parameters: Iterable[Iterable[object]]
        ) -> sqlite3.Cursor:
            self.executemany_count += 1
            return raw_connection.executemany(sql, parameters)

    counting = CountingConnection()
    try:
        raw_connection.execute('DELETE FROM match_search')
        service_module._rebuild_match_search(counting, (1, 2))
        raw_connection.commit()
        rows = raw_connection.execute(
            'SELECT match_id,COUNT(*) FROM match_search '
            'GROUP BY match_id ORDER BY match_id'
        ).fetchall()
    finally:
        raw_connection.close()

    assert counting.execute_count == 5
    assert counting.executemany_count == 1
    assert [tuple(row) for row in rows] == [(1, 7), (2, 7)]


def test_bootstrap_bulk_writes_matches_and_search_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    first = cache_batch(source_revision=8)
    first['publish'] = False
    first['reset'] = True

    def unexpected_row_writes(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('bootstrap used per-match database writes')

    monkeypatch.setattr(service_module, '_upsert_match', unexpected_row_writes)
    monkeypatch.setattr(service_module, '_rebuild_match_search', unexpected_row_writes)

    result = apply_ingest_batch(
        database_path,
        idempotency_key='bootstrap-bulk-first',
        batch=IngestBatch.parse_obj(first),
    )
    connection = connect_database(database_path)
    try:
        counts = connection.execute(
            'SELECT (SELECT COUNT(*) FROM matches),'
            '(SELECT COUNT(*) FROM match_teams),'
            '(SELECT COUNT(*) FROM match_participants),'
            '(SELECT COUNT(*) FROM match_search)'
        ).fetchone()
    finally:
        connection.close()

    assert result['matchCount'] == 1
    assert tuple(int(value) for value in counts) == (1, 2, 2, 7)


def test_publisher_sync_reaches_the_authenticated_api_ingest_seam(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / 'source.sqlite3'
    with sqlite3.connect(source_path) as connection:
        connection.execute(
            'CREATE TABLE dashboard_source_state('
            'singleton_id INTEGER PRIMARY KEY,revision INTEGER NOT NULL)'
        )
        connection.execute(
            'INSERT INTO dashboard_source_state(singleton_id,revision) VALUES(1,11)'
        )
    database_path = tmp_path / 'dashboard.sqlite3'
    settings = ApiSettings(
        database_path=database_path,
        source_database_path=source_path,
        ingest_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        cors_origins=('https://vg.luwei.host',),
        repository_mode='incremental',
    )
    source = cache_batch(source_revision=11)
    original = source['matches'][0]
    duplicate = json.loads(json.dumps(original))
    duplicate['id'] = 2
    duplicate['statsEligible'] = False
    duplicate['duplicateOfMatchId'] = 1
    duplicate['duplicateReviewState'] = 'confirmed'
    source['matches'] = [duplicate, original]
    source['sourceLastMatchId'] = 2

    with TestClient(
        create_app(settings, repository=_StaticRepository())  # type: ignore[arg-type]
    ) as client:

        def post_batch(key: str, content: bytes) -> Mapping[str, Any]:
            response = client.post(
                '/v1/cache/batches',
                content=content,
                headers={
                    'Authorization': 'Bearer {}'.format(TOKEN),
                    'Content-Type': 'application/json',
                    'X-Idempotency-Key': key,
                },
            )
            assert response.status_code == 200
            return json.loads(response.content)

        result = sync_dashboard_cache_once(
            database_path=source_path,
            state_directory=tmp_path / 'publisher-state',
            post_batch=post_batch,
            source_builder=lambda _connection: source,
            max_batch_matches=1,
        )

    repository = NormalizedDashboardRepository(
        source_target=source_path,
        auxiliary_target=database_path,
        revision_loader=lambda _target: 11,
    )
    repository.refresh(force=True)
    listed = repository.list_matches(
        page=1,
        page_size=10,
        season=None,
        mode=None,
        player_id=None,
        query='',
        heroes=(),
        rating_scope='all',
        rating_season=None,
        owner_view=True,
    )

    assert result.synced is True
    assert result.source_revision == 11
    assert listed['total'] == 2
    assert listed['items'][0]['id'] == 2
    assert listed['items'][0]['duplicateOfMatchId'] == 1
