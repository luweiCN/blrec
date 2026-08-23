from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pytest
from blrec_dashboard_api import direct as direct_module
from blrec_dashboard_api.app import create_app
from blrec_dashboard_api.dashboard import _ranked_trend_rows
from blrec_dashboard_api.dashboard_cache import (
    PostgresDashboardRepository,
    publish_dashboard_cache,
)
from blrec_dashboard_api.database import (
    _migration_text,
    connect_database,
    initialize_database,
)
from blrec_dashboard_api.direct import DirectDashboardRepository, _rating_trends
from blrec_dashboard_api.models import IngestBatch
from blrec_dashboard_api.normalized_repository import NormalizedDashboardRepository
from blrec_dashboard_api.replay_visibility import (
    complete_replay_visibility,
    resolve_match_replays,
)
from blrec_dashboard_api.service import apply_ingest_batch
from blrec_dashboard_api.settings import ApiSettings
from blrec_dashboard_publisher.snapshot import build_dashboard_snapshot_from_records
from fastapi.testclient import TestClient

TOKEN = 'test-asset-token'
OWNER_TOKEN = 'test-owner-token'


def _runtime_source(
    *,
    match_id: int = 1,
    result: str = 'W',
    public_visible: bool = True,
    replay_access: str = 'public',
    stats_eligible: bool = True,
    duplicate_of_match_id: Optional[int] = None,
    duplicate_review_state: str = 'none',
) -> Mapping[str, Any]:
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
        'stats_eligible': stats_eligible,
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
    public_snapshot = build_dashboard_snapshot_from_records(
        players=({7: {'name': '主播', 'rooms': [123456]}} if public_visible else {}),
        aliases={7: ['-Anchor-']} if public_visible else {},
        rows=[row] if public_visible else [],
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
        'publicSnapshot': public_snapshot,
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
                'publicVisible': public_visible,
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
                'duplicateOfMatchId': duplicate_of_match_id,
                'duplicateReviewState': duplicate_review_state,
                'ally': teams[0],
                'enemy': teams[1],
                'replay': {
                    'kind': 'match',
                    'url': 'https://www.bilibili.com/video/BV1test00001?t=120',
                },
                'replayAccess': replay_access,
            }
        ],
    }


def _repository(
    tmp_path: Path, *, runtime: Optional[Mapping[str, Any]] = None
) -> tuple[DirectDashboardRepository, list[int]]:
    auxiliary = tmp_path / 'public.sqlite3'
    initialize_database(auxiliary)
    revisions = [1]
    loads: list[int] = []

    def revision_loader(_target: Any) -> int:
        return revisions[-1]

    def runtime_loader(_target: Any) -> tuple[int, Mapping[str, Any]]:
        loads.append(revisions[-1])
        return revisions[-1], (runtime or _runtime_source(match_id=revisions[-1]))

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
        owner_token_sha256=hashlib.sha256(OWNER_TOKEN.encode()).hexdigest(),
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


def test_repository_releases_unused_pages_after_replacing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _loads = _repository(tmp_path)
    released = []
    monkeypatch.setattr(
        direct_module, '_release_unused_process_memory', lambda: released.append(True)
    )

    assert repository.refresh() is False
    assert released == []

    repository._revision_loader = lambda _target: 2
    repository._runtime_loader = lambda _target: (2, _runtime_source(match_id=2))

    assert repository.refresh() is True
    assert released == [True]


def test_public_dataset_reuses_the_owner_rating_index(tmp_path: Path) -> None:
    repository, _loads = _repository(tmp_path)

    public = repository._current()
    owner = repository._current(owner_view=True)

    assert public.ratings is owner.ratings


def test_repository_retains_dashboard_as_serialized_bytes(tmp_path: Path) -> None:
    repository, _loads = _repository(tmp_path)

    payload, revision = repository.dashboard_payload()

    assert revision == '1'
    assert isinstance(payload, bytes)
    assert json.loads(payload)['snapshot']['sourceMatchCount'] == 1
    assert not hasattr(repository._current(), 'document')


def test_postgres_cache_repository_preserves_the_direct_repository_contract(
    tmp_path: Path,
) -> None:
    direct, _loads = _repository(tmp_path)
    assert direct._state is not None
    publish_dashboard_cache(tmp_path / 'public.sqlite3', direct._state, published_at=1)
    cached = PostgresDashboardRepository(
        source_target=tmp_path / 'unused.sqlite3',
        auxiliary_target=tmp_path / 'public.sqlite3',
        revision_loader=lambda _target: 1,
    )
    cached.refresh(force=True)

    assert cached.dashboard_payload() == direct.dashboard_payload()
    assert cached.dashboard_payload(owner_view=True) == direct.dashboard_payload(
        owner_view=True
    )
    assert cached.live_rooms() == direct.live_rooms()
    assert cached.match_summary(
        season=None, mode=None, player_id=None
    ) == direct.match_summary(season=None, mode=None, player_id=None)

    query = {
        'page': 1,
        'page_size': 10,
        'season': '2026-summer',
        'mode': '3v3',
        'player_id': 7,
        'query': 'zhuboshenye',
        'heroes': ('剑圣', '猫女'),
        'rating_scope': '3v3',
        'rating_season': '2026-summer',
        'owner_view': True,
    }
    assert cached.list_matches(**query) == direct.list_matches(**query)
    public_query = dict(query)
    public_query['owner_view'] = False
    assert cached.list_matches(**public_query) == direct.list_matches(**public_query)
    assert cached.get_match(
        1, rating_scope='3v3', rating_season='2026-summer', owner_view=True
    ) == direct.get_match(
        1, rating_scope='3v3', rating_season='2026-summer', owner_view=True
    )


def test_incremental_repository_preserves_the_direct_repository_contract(
    tmp_path: Path,
) -> None:
    runtime = _runtime_source()
    direct, _loads = _repository(tmp_path, runtime=runtime)
    matches = [dict(value, statsEligible=True) for value in runtime['matches']]
    apply_ingest_batch(
        tmp_path / 'public.sqlite3',
        idempotency_key='incremental-parity-1',
        batch=IngestBatch.parse_obj(
            {
                'schemaVersion': 2,
                'sourceRevision': 1,
                'publish': True,
                'generatedAt': runtime['snapshot']['generatedAt'],
                'sourceLastMatchId': runtime['snapshot']['sourceLastMatchId'],
                'players': runtime['players'],
                'matches': matches,
                'removedMatchIds': [],
            }
        ),
    )
    incremental = NormalizedDashboardRepository(
        source_target=tmp_path / 'unused.sqlite3',
        auxiliary_target=tmp_path / 'public.sqlite3',
        revision_loader=lambda _target: 1,
    )
    incremental.refresh(force=True)

    assert incremental.dashboard_payload() == direct.dashboard_payload()
    assert incremental.dashboard_payload(owner_view=True) == direct.dashboard_payload(
        owner_view=True
    )
    assert incremental.live_rooms() == direct.live_rooms()
    assert incremental.match_summary(
        season=None, mode=None, player_id=None
    ) == direct.match_summary(season=None, mode=None, player_id=None)

    query = {
        'page': 1,
        'page_size': 10,
        'season': '2026-summer',
        'mode': '3v3',
        'player_id': 7,
        'query': 'zhuboshenye',
        'heroes': ('剑圣', '猫女'),
        'rating_scope': '3v3',
        'rating_season': '2026-summer',
        'owner_view': True,
    }
    assert incremental.list_matches(**query) == direct.list_matches(**query)
    public_query = dict(query)
    public_query['owner_view'] = False
    assert incremental.list_matches(**public_query) == direct.list_matches(
        **public_query
    )
    assert incremental.get_match(
        1, rating_scope='3v3', rating_season='2026-summer', owner_view=True
    ) == direct.get_match(
        1, rating_scope='3v3', rating_season='2026-summer', owner_view=True
    )


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


def test_duplicate_match_remains_visible_without_a_rating(tmp_path: Path) -> None:
    repository, _loads = _repository(
        tmp_path,
        runtime=_runtime_source(stats_eligible=False, duplicate_of_match_id=99),
    )

    document, _revision = repository.dashboard_document()
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
    )

    assert document['snapshot']['sourceMatchCount'] == 0
    assert listed['total'] == 1
    assert listed['items'][0]['duplicateOfMatchId'] == 99
    assert listed['items'][0]['rating'] is None


def test_public_cache_shares_safe_matches_but_copies_private_replays(
    tmp_path: Path,
) -> None:
    public_repository, _loads = _repository(tmp_path)
    public_state = public_repository._current()
    owner_state = public_repository._current(owner_view=True)

    assert public_state.matches[0] is owner_state.matches[0]

    private_repository, _loads = _repository(
        tmp_path, runtime=_runtime_source(replay_access='owner')
    )
    private_public = private_repository._current()
    private_owner = private_repository._current(owner_view=True)

    assert private_public.matches[0] is not private_owner.matches[0]
    assert 'replay' not in private_public.matches[0]
    assert 'replay' in private_owner.matches[0]


def test_repository_keeps_hidden_players_only_in_the_owner_view(tmp_path: Path) -> None:
    repository, _loads = _repository(
        tmp_path, runtime=_runtime_source(public_visible=False, replay_access='owner')
    )

    public_document, _revision = repository.dashboard_document()
    owner_document, _revision = repository.dashboard_document(owner_view=True)
    public_matches = repository.list_matches(
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
    owner_matches = repository.list_matches(
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

    assert public_document['snapshot']['sourceMatchCount'] == 0
    assert owner_document['snapshot']['sourceMatchCount'] == 1
    assert public_matches['total'] == 0
    assert owner_matches['total'] == 1
    assert owner_matches['items'][0]['replay']['kind'] == 'match'


def test_owner_auth_unlocks_private_replays_without_public_cache_leak(
    tmp_path: Path,
) -> None:
    repository, _loads = _repository(
        tmp_path, runtime=_runtime_source(replay_access='owner')
    )
    client = TestClient(create_app(_settings(tmp_path), repository=repository))

    public = client.get('/v1/matches')
    invalid = client.get('/v1/matches', headers={'Authorization': 'Bearer wrong-token'})
    owner = client.get(
        '/v1/matches', headers={'Authorization': 'Bearer {}'.format(OWNER_TOKEN)}
    )
    owner_dashboard = client.get(
        '/v1/dashboard', headers={'Authorization': 'Bearer {}'.format(OWNER_TOKEN)}
    )
    owner_session = client.get(
        '/v1/owner/session', headers={'Authorization': 'Bearer {}'.format(OWNER_TOKEN)}
    )

    assert public.status_code == 200
    assert 'replay' not in public.json()['items'][0]
    assert invalid.status_code == 401
    assert owner.status_code == 200
    assert owner.json()['items'][0]['replay']['url'].startswith(
        'https://www.bilibili.com/'
    )
    assert owner.headers['cache-control'] == 'private, no-store'
    assert owner_dashboard.headers['cache-control'] == 'private, no-store'
    assert 'etag' not in owner_dashboard.headers
    assert owner_session.json() == {'owner': True}
    assert owner_session.headers['cache-control'] == 'private, no-store'


def test_public_match_replay_is_checked_once_and_uses_the_persistent_cache(
    tmp_path: Path,
) -> None:
    repository, _loads = _repository(tmp_path)

    first = repository.list_matches(
        page=1,
        page_size=20,
        season=None,
        mode=None,
        player_id=None,
        query='',
        heroes=(),
        rating_scope='all',
        rating_season=None,
    )
    second = repository.list_matches(
        page=1,
        page_size=20,
        season=None,
        mode=None,
        player_id=None,
        query='',
        heroes=(),
        rating_scope='all',
        rating_season=None,
    )
    connection = connect_database(tmp_path / 'public.sqlite3')
    try:
        queued = int(
            connection.execute(
                'SELECT COUNT(*) FROM replay_visibility_checks'
            ).fetchone()[0]
        )
    finally:
        connection.close()

    assert first['items'][0]['replayStatus'] == 'checking'
    assert 'replay' not in first['items'][0]
    assert 'BV1test00001' not in json.dumps(first['items'][0])
    assert second['items'][0]['replayStatus'] == 'checking'
    assert queued == 1

    complete_replay_visibility(
        tmp_path / 'public.sqlite3', 'BV1test00001', public_visible=True
    )
    available = repository.get_match(1, rating_scope='all', rating_season=None)

    assert available['replayStatus'] == 'available'
    assert available['replay']['url'].endswith('BV1test00001?t=120')

    connection = connect_database(tmp_path / 'public.sqlite3')
    try:
        cache = connection.execute(
            'SELECT checked_at,expires_at FROM replay_visibility_checks '
            'WHERE bvid=?',
            ('BV1test00001',),
        ).fetchone()
        assert cache is not None
        assert int(cache['expires_at']) - int(cache['checked_at']) == 15 * 60
        connection.execute(
            'UPDATE replay_visibility_checks SET expires_at=0 WHERE bvid=?',
            ('BV1test00001',),
        )
        connection.commit()
    finally:
        connection.close()

    expired = repository.get_match(1, rating_scope='all', rating_season=None)
    assert expired['replayStatus'] == 'checking'
    assert 'replay' not in expired


def test_public_match_hides_a_fresh_unavailable_replay_but_owner_keeps_it(
    tmp_path: Path,
) -> None:
    repository, _loads = _repository(tmp_path)
    repository.list_matches(
        page=1,
        page_size=20,
        season=None,
        mode=None,
        player_id=None,
        query='',
        heroes=(),
        rating_scope='all',
        rating_season=None,
    )
    complete_replay_visibility(
        tmp_path / 'public.sqlite3', 'BV1test00001', public_visible=False
    )

    public = repository.get_match(1, rating_scope='all', rating_season=None)
    owner = repository.get_match(
        1, rating_scope='all', rating_season=None, owner_view=True
    )

    assert public['replayStatus'] == 'unavailable'
    assert 'replay' not in public
    assert owner['replayStatus'] == 'available'
    assert owner['replay']['url'].startswith('https://www.bilibili.com/')


def test_same_archive_on_a_page_creates_only_one_visibility_task(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'public.sqlite3'
    initialize_database(database_path)
    replay = {
        'kind': 'match',
        'url': 'https://www.bilibili.com/video/BV1test00001?p=2&t=120',
    }

    values = resolve_match_replays(
        database_path,
        ({'id': 1}, {'id': 2}),
        {1: {'replay': replay}, 2: {'replay': replay}},
        owner_view=False,
    )
    connection = connect_database(database_path)
    try:
        queued = int(
            connection.execute(
                'SELECT COUNT(*) FROM replay_visibility_checks'
            ).fetchone()[0]
        )
    finally:
        connection.close()

    assert [value['replayStatus'] for value in values] == ['checking', 'checking']
    assert queued == 1


def test_replay_visibility_worker_endpoints_require_ingest_authentication(
    tmp_path: Path,
) -> None:
    repository, _loads = _repository(tmp_path)
    client = TestClient(create_app(_settings(tmp_path), repository=repository))
    client.get('/v1/matches?pageSize=20')

    unauthorized = client.post('/v1/replay-visibility/claim?waitSeconds=0')
    claimed = client.post(
        '/v1/replay-visibility/claim?waitSeconds=0',
        headers={'Authorization': 'Bearer {}'.format(TOKEN)},
    )
    completed = client.post(
        '/v1/replay-visibility/BV1test00001/complete',
        headers={'Authorization': 'Bearer {}'.format(TOKEN)},
        json={'publicVisible': True},
    )
    public = client.get('/v1/matches/1').json()

    assert unauthorized.status_code == 401
    assert claimed.status_code == 200
    assert claimed.json() == {'bvid': 'BV1test00001'}
    assert completed.status_code == 200
    assert completed.json()['state'] == 'public'
    assert public['replayStatus'] == 'available'


def test_rating_trend_uses_the_match_date_instead_of_the_calculation_date(
    tmp_path: Path,
) -> None:
    repository, _loads = _repository(tmp_path)
    document, _revision = repository.dashboard_document()
    publications = document['trends']['publications']
    current = document['snapshot']

    assert [value['publicationDate'] for value in publications] == [
        '2026-06-01',
        '2026-08-11',
    ]
    assert publications[-1]['snapshotId'] == current['snapshotId']
    historical = publications[0]['standings']['2026-summer']['3v3'][0]
    latest = publications[-1]['standings']['2026-summer']['3v3'][0]
    assert historical['ratingScore'] == latest['ratingScore']


def test_current_trend_ranks_by_season_peak_but_keeps_the_current_score() -> None:
    def player(
        player_id: int, *, peak_score: float, current_score: float
    ) -> Mapping[str, Any]:
        return {
            'id': player_id,
            'modes': {
                '3v3': {
                    'ratingScore': peak_score,
                    'currentRatingScore': current_score,
                    'matches': 10,
                    'wins': 6,
                }
            },
        }

    standings = _ranked_trend_rows(
        [
            player(1, peak_score=940, current_score=900),
            player(2, peak_score=930, current_score=920),
        ],
        '3v3',
    )

    assert standings == [
        {'playerId': 1, 'rank': 1, 'ratingScore': 900},
        {'playerId': 2, 'rank': 2, 'ratingScore': 920},
    ]


def test_historical_trend_rank_keeps_the_season_peak_after_a_loss() -> None:
    snapshot = _runtime_source()['snapshot']
    matches = [
        {
            'id': 1,
            'playerId': 1,
            'seasonKey': '2026-summer',
            'mode': '3v3',
            'playedAt': '2026-06-01T01:00:00Z',
            'result': 'W',
        },
        {
            'id': 2,
            'playerId': 2,
            'seasonKey': '2026-summer',
            'mode': '3v3',
            'playedAt': '2026-06-01T02:00:00Z',
            'result': 'W',
        },
        {
            'id': 3,
            'playerId': 1,
            'seasonKey': '2026-summer',
            'mode': '3v3',
            'playedAt': '2026-06-02T01:00:00Z',
            'result': 'L',
        },
    ]
    ratings = {}
    for match_id, before, after in ((1, 900, 910), (2, 900, 905), (3, 910, 880)):
        for scope in ('all', '3v3'):
            ratings[(match_id, scope, '2026-summer')] = {
                'scoreBefore': before,
                'scoreAfter': after,
            }

    trends = _rating_trends(snapshot, matches, ratings)
    standings = next(
        publication['standings']['2026-summer']['3v3']
        for publication in trends['publications']
        if publication['publicationDate'] == '2026-06-02'
    )

    assert standings == [
        {'playerId': 1, 'rank': 1, 'ratingScore': 880 / 3},
        {'playerId': 2, 'rank': 2, 'ratingScore': 905 / 3},
    ]


def test_rating_trends_keep_only_the_latest_frontend_supported_publications() -> None:
    snapshot = _runtime_source()['snapshot']
    matches = []
    ratings = {}
    started_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for index in range(181):
        match_id = index + 1
        match = {
            'id': match_id,
            'playerId': 7,
            'seasonKey': '2025-spring',
            'mode': '3v3',
            'playedAt': (started_at + timedelta(days=index)).isoformat(),
            'result': 'W',
        }
        matches.append(match)
        for scope in ('all', '3v3'):
            for season_key in ('2025-spring', 'all-time'):
                ratings[(match_id, scope, season_key)] = {'scoreAfter': 1000 + index}

    trends = _rating_trends(snapshot, matches, ratings)

    assert len(trends['publications']) == 180
    assert trends['publications'][0]['publicationDate'] == '2025-01-03'
    assert trends['publications'][-1]['snapshotId'] == snapshot['snapshotId']


def test_schema_upgrade_discards_legacy_trend_cache(tmp_path: Path) -> None:
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
        legacy = upgraded.execute(
            'SELECT publication_date FROM dashboard_trend_publications'
        ).fetchone()
    finally:
        upgraded.close()

    assert publication is None
    assert standing is None
    assert legacy is None


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
