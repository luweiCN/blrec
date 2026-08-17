from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from blrec_dashboard_api.assets import apply_asset_batch
from blrec_dashboard_api.database import connect_database, initialize_database
from blrec_dashboard_api.migrate_sqlite_to_postgres import migrate
from blrec_dashboard_api.models import AssetBatch

_DATA_TABLES = (
    'dashboard_publication_standings',
    'dashboard_publications',
    'match_assets',
    'asset_batches',
)


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
