from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from blrec_dashboard_api.assets import apply_asset_batch
from blrec_dashboard_api.dashboard_cache import (
    PostgresDashboardRepository,
    publish_dashboard_cache,
)
from blrec_dashboard_api.database import (
    PostgresConnection,
    connect_database,
    initialize_database,
)
from blrec_dashboard_api.migrate_sqlite_to_postgres import migrate
from blrec_dashboard_api.models import AssetBatch

_DATA_TABLES = (
    'dashboard_cache_state',
    'dashboard_cache_generations',
    'dashboard_publication_standings',
    'dashboard_publications',
    'match_assets',
    'asset_batches',
    'replay_visibility_checks',
)


def test_postgres_connection_streams_rows_with_copy() -> None:
    written: list[tuple[int, str]] = []
    statements: list[str] = []

    class Copy:
        def __enter__(self) -> 'Copy':
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write_row(self, row: tuple[int, str]) -> None:
            written.append(row)

    class Cursor:
        def copy(self, statement: str) -> Copy:
            statements.append(statement)
            return Copy()

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    connection = PostgresConnection(Connection(), SimpleNamespace())

    connection.copy_rows('COPY cache(id,value) FROM STDIN', ((1, 'a'), (2, 'b')))

    assert statements == ['COPY cache(id,value) FROM STDIN']
    assert written == [(1, 'a'), (2, 'b')]


def test_deployment_backup_is_limited_to_public_schema() -> None:
    script = (
        Path(__file__).resolve().parents[1] / 'deploy' / 'install-release.sh'
    ).read_text(encoding='utf-8')

    assert 'pg_dump --schema=public --format=custom' in script
    assert 'for _attempt in {1..120}; do' in script


def _empty_postgres(database_url: str) -> None:
    initialize_database(database_url)
    connection = connect_database(database_url)
    try:
        for table in _DATA_TABLES:
            connection.execute('DELETE FROM {}'.format(table))
        connection.commit()
    finally:
        connection.close()


def _asset_batch() -> AssetBatch:
    return AssetBatch.parse_obj(
        {
            'schemaVersion': 1,
            'generatedAt': '2026-08-11T10:30:00+08:00',
            'images': [
                {
                    'matchId': 9,
                    'url': 'https://vg.luwei.host/data/match-images/9.webp',
                    'width': 1600,
                    'height': 900,
                    'sha256': 'a' * 64,
                }
            ],
            'removedMatchIds': [],
        }
    )


def _cache_state() -> SimpleNamespace:
    match = {
        'id': 1,
        'playerId': 7,
        'seasonKey': '2026-summer',
        'mode': '3v3',
        'playedAt': '2026-06-01T00:00:00Z',
        'durationSeconds': 900,
        'result': 'W',
    }
    dataset = SimpleNamespace(
        source_revision=7,
        dashboard_payload=json.dumps(
            {'snapshot': {'sourceMatchCount': 1}, 'trends': {'publications': []}},
            separators=(',', ':'),
        ).encode(),
        players={7: {'id': 7, 'name': '主播'}},
        matches=(match,),
        search_forms={1: (('主播', 'zhubo', 'zb'),)},
        heroes={1: frozenset(('剑圣',))},
        ratings={(1, 'all', '2026-summer'): {'scoreAfter': 625}},
        live_rooms={
            'schemaVersion': 1,
            'updatedAt': '2026-06-01T00:00:00Z',
            'rooms': [],
        },
    )
    return SimpleNamespace(public=dataset, owner=dataset)


@pytest.mark.skipif(
    not os.environ.get('DASHBOARD_TEST_POSTGRES_URL'),
    reason='DASHBOARD_TEST_POSTGRES_URL is not configured',
)
def test_postgres_backend_applies_transactional_asset_batches() -> None:
    database_url = os.environ['DASHBOARD_TEST_POSTGRES_URL']
    database_name = urlsplit(database_url).path.lstrip('/')
    if not database_name.endswith('_test'):
        raise AssertionError('PostgreSQL integration tests require a _test database')
    _empty_postgres(database_url)

    result = apply_asset_batch(
        database_url, idempotency_key='postgres-asset', batch=_asset_batch()
    )
    connection = connect_database(database_url)
    try:
        count = int(
            connection.execute('SELECT COUNT(*) FROM match_assets').fetchone()[0]
        )
    finally:
        connection.close()

    assert result['status'] == 'applied'
    assert count == 1


@pytest.mark.skipif(
    not os.environ.get('DASHBOARD_TEST_POSTGRES_URL'),
    reason='DASHBOARD_TEST_POSTGRES_URL is not configured',
)
def test_postgres_backend_streams_dashboard_cache_rows_with_copy() -> None:
    database_url = os.environ['DASHBOARD_TEST_POSTGRES_URL']
    if not urlsplit(database_url).path.lstrip('/').endswith('_test'):
        raise AssertionError('PostgreSQL integration tests require a _test database')
    _empty_postgres(database_url)

    publish_dashboard_cache(database_url, _cache_state(), published_at=100)
    repository = PostgresDashboardRepository(
        source_target=database_url,
        auxiliary_target=database_url,
        revision_loader=lambda _target: 7,
    )
    repository.refresh(force=True)

    assert repository.dashboard_payload()[1] == '7'
    assert (
        repository.list_matches(
            page=1,
            page_size=10,
            season=None,
            mode=None,
            player_id=None,
            query='zhubo',
            heroes=('剑圣',),
            rating_scope='all',
            rating_season=None,
            owner_view=True,
        )['total']
        == 1
    )


@pytest.mark.skipif(
    not os.environ.get('DASHBOARD_TEST_POSTGRES_URL'),
    reason='DASHBOARD_TEST_POSTGRES_URL is not configured',
)
def test_sqlite_auxiliary_rows_migrate_to_postgres(tmp_path: Path) -> None:
    database_url = os.environ['DASHBOARD_TEST_POSTGRES_URL']
    if not urlsplit(database_url).path.lstrip('/').endswith('_test'):
        raise AssertionError('PostgreSQL integration tests require a _test database')
    _empty_postgres(database_url)
    sqlite_path = tmp_path / 'dashboard.sqlite3'
    initialize_database(sqlite_path)
    apply_asset_batch(sqlite_path, idempotency_key='sqlite-asset', batch=_asset_batch())

    copied = migrate(sqlite_path, database_url, backup_directory=tmp_path / 'backups')

    assert copied['match_assets'] == 1
    assert copied['asset_batches'] == 1
