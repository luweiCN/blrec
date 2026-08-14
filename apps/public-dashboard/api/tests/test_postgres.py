from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from blrec_dashboard_api.app import create_app
from blrec_dashboard_api.database import connect_database, initialize_database
from blrec_dashboard_api.migrate_sqlite_to_postgres import migrate
from blrec_dashboard_api.settings import ApiSettings
from fastapi.testclient import TestClient
from test_api import TOKEN, batch, ingest, match

_DATA_TABLES = (
    'ingestion_batches',
    'removed_matches',
    'dashboard_trend_publications',
    'dashboard_state',
    'rating_events',
    'match_search',
    'match_participants',
    'match_teams',
    'matches',
    'player_live_rooms',
    'player_aliases',
    'player_rooms',
    'players',
)


def _empty_postgres(database_url: str) -> None:
    initialize_database(database_url)
    connection = connect_database(database_url)
    try:
        for table in _DATA_TABLES:
            connection.execute('DELETE FROM {}'.format(table))
        connection.commit()
    finally:
        connection.close()


@pytest.mark.skipif(
    not os.environ.get('DASHBOARD_TEST_POSTGRES_URL'),
    reason='DASHBOARD_TEST_POSTGRES_URL is not configured',
)
def test_postgres_backend_applies_ingest_and_serves_dashboard() -> None:
    database_url = os.environ['DASHBOARD_TEST_POSTGRES_URL']
    database_name = urlsplit(database_url).path.lstrip('/')
    if not database_name.endswith('_test'):
        raise AssertionError('PostgreSQL integration tests require a _test database')
    _empty_postgres(database_url)
    settings = ApiSettings(
        database_path=Path('/unused.sqlite3'),
        database_url=database_url,
        ingest_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        cors_origins=('https://vg.luwei.host',),
    )
    client = TestClient(create_app(settings))

    response = ingest(
        client, batch([match(1, played_at='2026-08-11T10:30:00+08:00', result='W')])
    )

    assert response.status_code == 200
    assert client.get('/v1/dashboard').status_code == 200
    assert client.get('/v1/matches').json()['total'] == 1


@pytest.mark.skipif(
    not os.environ.get('DASHBOARD_TEST_POSTGRES_URL'),
    reason='DASHBOARD_TEST_POSTGRES_URL is not configured',
)
def test_sqlite_database_migrates_without_losing_dashboard_rows(tmp_path: Path) -> None:
    database_url = os.environ['DASHBOARD_TEST_POSTGRES_URL']
    if not urlsplit(database_url).path.lstrip('/').endswith('_test'):
        raise AssertionError('PostgreSQL integration tests require a _test database')
    _empty_postgres(database_url)
    sqlite_path = tmp_path / 'dashboard.sqlite3'
    sqlite_settings = ApiSettings(
        database_path=sqlite_path,
        ingest_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        cors_origins=('https://vg.luwei.host',),
    )
    source_client = TestClient(create_app(sqlite_settings))
    assert (
        ingest(
            source_client,
            batch([match(9, played_at='2026-08-12T10:30:00+08:00', result='L')]),
        ).status_code
        == 200
    )

    copied = migrate(sqlite_path, database_url, backup_directory=tmp_path / 'backups')

    assert copied['matches'] == 1
    target_settings = ApiSettings(
        database_path=Path('/unused.sqlite3'),
        database_url=database_url,
        ingest_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        cors_origins=('https://vg.luwei.host',),
    )
    target_client = TestClient(create_app(target_settings))
    assert target_client.get('/v1/matches').json()['total'] == 1
    assert (
        target_client.get('/v1/dashboard').json()
        == source_client.get('/v1/dashboard').json()
    )
