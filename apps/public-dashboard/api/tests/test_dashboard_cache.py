from __future__ import annotations

import hashlib
import sqlite3
from json import dumps
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from blrec_dashboard_api.app import create_app
from blrec_dashboard_api.dashboard_cache import (
    PostgresDashboardRepository,
    publish_dashboard_cache,
)
from blrec_dashboard_api.database import connect_database, initialize_database
from blrec_dashboard_api.settings import ApiSettings
from fastapi.testclient import TestClient


def _dataset(source_revision: int, *, replay: bool = True) -> Any:
    match = {
        'id': 1,
        'playerId': 7,
        'seasonKey': '2026-summer',
        'mode': '3v3',
        'playedAt': '2026-06-01T00:00:00Z',
        'durationSeconds': 900,
        'result': 'W',
        'ally': {'players': [{'heroName': '剑圣'}]},
        'enemy': {'players': [{'heroName': '猫女'}]},
    }
    if replay:
        match['replay'] = {
            'kind': 'match',
            'url': 'https://www.bilibili.com/video/BV1test00001?t=10',
        }
    dashboard = {'snapshot': {'sourceMatchCount': 1}, 'trends': {'publications': []}}
    return SimpleNamespace(
        source_revision=source_revision,
        dashboard_payload=dumps(
            dashboard, ensure_ascii=False, separators=(',', ':')
        ).encode(),
        players={7: {'id': 7, 'name': '主播'}},
        matches=(match,),
        search_forms={1: (('主播', 'zhubo', 'zb'),)},
        heroes={1: frozenset(('剑圣', '猫女'))},
        ratings={
            (1, '3v3', '2026-summer'): {
                'scope': '3v3',
                'seasonKey': '2026-summer',
                'scoreAfter': 625,
            }
        },
        live_rooms={
            'schemaVersion': 1,
            'updatedAt': '2026-06-01T00:00:00Z',
            'rooms': [],
        },
    )


def _state(source_revision: int) -> Any:
    return SimpleNamespace(
        public=_dataset(source_revision), owner=_dataset(source_revision)
    )


def _empty_dataset(source_revision: int) -> Any:
    dataset = _dataset(source_revision, replay=False)
    dataset.dashboard_payload = dumps(
        {'snapshot': {'sourceMatchCount': 0}, 'trends': {'publications': []}},
        separators=(',', ':'),
    ).encode()
    dataset.players = {}
    dataset.matches = ()
    dataset.search_forms = {}
    dataset.heroes = {}
    dataset.ratings = {}
    return dataset


def test_cache_schema_enforces_revision_and_audience_boundaries(tmp_path: Path) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    connection = connect_database(database_path)
    try:
        tables = {
            str(row['name'])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            'dashboard_cache_generations',
            'dashboard_cache_players',
            'dashboard_cache_matches',
            'dashboard_cache_match_search',
            'dashboard_cache_match_heroes',
            'dashboard_cache_state',
        }.issubset(tables)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO dashboard_cache_generations('
                'source_revision,audience,dashboard_payload,live_rooms_payload,'
                "published_at) VALUES(1,'invalid','{}','{}',1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO dashboard_cache_generations('
                'source_revision,audience,dashboard_payload,live_rooms_payload,'
                "published_at) VALUES(1,'public',X'00','{}',1)"
            )
    finally:
        connection.close()


def test_cache_publication_switches_both_audiences_atomically(tmp_path: Path) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)

    publish_dashboard_cache(database_path, _state(7), published_at=100)
    connection = connect_database(database_path)
    try:
        pointers = connection.execute(
            'SELECT audience,source_revision FROM dashboard_cache_state '
            'ORDER BY audience'
        ).fetchall()
        matches = int(
            connection.execute(
                'SELECT COUNT(*) FROM dashboard_cache_matches '
                'WHERE source_revision=7'
            ).fetchone()[0]
        )
    finally:
        connection.close()

    assert [tuple(row) for row in pointers] == [('owner', 7), ('public', 7)]
    assert matches == 2

    invalid = SimpleNamespace(public=_dataset(8), owner=_dataset(9))
    with pytest.raises(ValueError, match='same source revision'):
        publish_dashboard_cache(database_path, invalid, published_at=101)

    connection = connect_database(database_path)
    try:
        revisions = {
            int(row['source_revision'])
            for row in connection.execute(
                'SELECT source_revision FROM dashboard_cache_state'
            ).fetchall()
        }
    finally:
        connection.close()
    assert revisions == {7}


def test_cache_publication_cannot_move_the_active_revision_backwards(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    publish_dashboard_cache(database_path, _state(8), published_at=100)

    with pytest.raises(ValueError, match='newer revision'):
        publish_dashboard_cache(database_path, _state(7), published_at=101)

    connection = connect_database(database_path)
    try:
        revisions = {
            int(row['source_revision'])
            for row in connection.execute(
                'SELECT source_revision FROM dashboard_cache_state'
            ).fetchall()
        }
    finally:
        connection.close()
    assert revisions == {8}


def test_postgres_repository_keeps_only_payloads_and_queries_matches(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    publish_dashboard_cache(database_path, _state(7), published_at=100)
    repository = PostgresDashboardRepository(
        source_target=database_path,
        auxiliary_target=database_path,
        revision_loader=lambda _target: 7,
    )

    assert repository.refresh(force=True) is True
    payload, revision = repository.dashboard_payload()
    listed = repository.list_matches(
        page=1,
        page_size=10,
        season='2026-summer',
        mode='3v3',
        player_id=7,
        query='zhubo',
        heroes=('剑圣', '猫女'),
        rating_scope='3v3',
        rating_season=None,
    )

    assert revision == '7'
    assert payload.startswith(b'{"snapshot"')
    assert listed['total'] == 1
    assert listed['items'][0]['player']['name'] == '主播'
    assert listed['items'][0]['rating']['scoreAfter'] == 625
    assert not hasattr(repository._current(), 'matches')
    assert repository.match_summary(season=None, mode=None, player_id=None) == {
        'matches': 1,
        'wins': 1,
        'players': 1,
        'averageDurationSeconds': 900,
        'replays': 1,
    }


def test_postgres_repository_rebuilds_before_loading_a_stale_revision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    publish_dashboard_cache(database_path, _state(7), published_at=100)
    builds: list[int] = []

    def build() -> int:
        builds.append(8)
        return publish_dashboard_cache(database_path, _state(8), published_at=101)

    repository = PostgresDashboardRepository(
        source_target=database_path,
        auxiliary_target=database_path,
        revision_loader=lambda _target: 8,
        cache_builder=build,
    )

    assert repository.refresh(force=True) is True
    assert repository.dashboard_payload()[1] == '8'
    assert builds == [8]


def test_postgres_repository_accepts_a_builder_revision_ahead_of_its_probe(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    publish_dashboard_cache(database_path, _state(7), published_at=100)
    builds: list[int] = []

    def build() -> int:
        builds.append(9)
        return publish_dashboard_cache(database_path, _state(9), published_at=101)

    repository = PostgresDashboardRepository(
        source_target=database_path,
        auxiliary_target=database_path,
        revision_loader=lambda _target: 8,
        cache_builder=build,
    )

    assert repository.refresh(force=True) is True
    assert repository.dashboard_payload()[1] == '9'
    assert builds == [9]


def test_postgres_repository_physically_separates_public_and_owner_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    state = SimpleNamespace(public=_empty_dataset(7), owner=_dataset(7))
    publish_dashboard_cache(database_path, state, published_at=100)
    repository = PostgresDashboardRepository(
        source_target=database_path,
        auxiliary_target=database_path,
        revision_loader=lambda _target: 7,
    )
    repository.refresh(force=True)

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

    assert public['items'] == []
    assert public['total'] == 0
    assert owner['total'] == 1
    assert owner['items'][0]['replayStatus'] == 'available'
    with pytest.raises(LookupError, match='not found'):
        repository.get_match(1, rating_scope='all', rating_season=None)


def test_app_can_serve_the_verified_postgres_cache_mode(tmp_path: Path) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(database_path)
    connection = connect_database(database_path)
    try:
        connection.execute(
            'CREATE TABLE dashboard_source_state('
            'singleton_id INTEGER PRIMARY KEY,revision INTEGER NOT NULL)'
        )
        connection.execute(
            'INSERT INTO dashboard_source_state(singleton_id,revision) VALUES(1,7)'
        )
        connection.commit()
    finally:
        connection.close()
    publish_dashboard_cache(database_path, _state(7), published_at=100)
    settings = ApiSettings(
        database_path=database_path,
        source_database_path=database_path,
        ingest_token_sha256=hashlib.sha256(b'token').hexdigest(),
        cors_origins=('https://vg.luwei.host',),
        repository_mode='postgres',
    )

    client = TestClient(create_app(settings))

    assert client.get('/v1/dashboard').status_code == 200
    assert client.get('/v1/matches').json()['total'] == 1
    assert client.get('/v1/matches/summary').json()['matches'] == 1
