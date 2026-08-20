from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from blrec_dashboard_publisher.cache_sync import sync_dashboard_cache_once


def _source(matches: int) -> Mapping[str, Any]:
    return {
        'schemaVersion': 2,
        'generatedAt': '2026-08-20T00:00:00Z',
        'sourceLastMatchId': matches,
        'players': [{'id': 7, 'name': '主播'}],
        'matches': [{'id': match_id} for match_id in range(1, matches + 1)],
    }


def _database(path: Path, revision: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TABLE dashboard_source_state('
            'singleton_id INTEGER PRIMARY KEY,revision INTEGER NOT NULL)'
        )
        connection.execute(
            'INSERT INTO dashboard_source_state(singleton_id,revision) VALUES(1,?)',
            (revision,),
        )


def test_cache_sync_bootstraps_in_bounded_durable_batches(tmp_path: Path) -> None:
    database_path = tmp_path / 'source.sqlite3'
    state_directory = tmp_path / 'state'
    _database(database_path, 7)
    posted = []

    def post_batch(key: str, content: bytes) -> Mapping[str, Any]:
        posted.append((key, json.loads(content)))
        return {'status': 'applied'}

    result = sync_dashboard_cache_once(
        database_path=database_path,
        state_directory=state_directory,
        post_batch=post_batch,
        source_builder=lambda _connection: _source(1201),
        max_batch_matches=500,
    )

    assert result.synced is True
    assert result.batch_count == 3
    assert result.match_count == 1201
    assert [len(batch['matches']) for _key, batch in posted] == [500, 500, 201]
    assert [batch['reset'] for _key, batch in posted] == [True, False, False]
    assert [batch['publish'] for _key, batch in posted] == [False, False, True]
    assert all(batch['sourceRevision'] == 7 for _key, batch in posted)
    state = json.loads(
        (state_directory / 'cache-sync-state.json').read_text(encoding='utf-8')
    )
    assert state['sourceRevision'] == 7
    assert len(state['matches']) == 1201
    assert list((state_directory / 'cache-api-outbox').glob('*.json')) == []


def test_cache_sync_bootstrap_orders_duplicate_sources_before_references(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'source.sqlite3'
    state_directory = tmp_path / 'state'
    _database(database_path, 7)
    posted = []
    source = _source(0)
    source['matches'] = [
        {'id': 3, 'duplicateOfMatchId': 1},
        {'id': 2, 'duplicateOfMatchId': None},
        {'id': 1, 'duplicateOfMatchId': None},
    ]

    def post_batch(key: str, content: bytes) -> Mapping[str, Any]:
        posted.append((key, json.loads(content)))
        return {'status': 'applied'}

    sync_dashboard_cache_once(
        database_path=database_path,
        state_directory=state_directory,
        post_batch=post_batch,
        source_builder=lambda _connection: source,
        max_batch_matches=2,
    )

    match_ids = [match['id'] for _key, batch in posted for match in batch['matches']]
    assert match_ids.index(1) < match_ids.index(3)
    assert [len(batch['matches']) for _key, batch in posted] == [2, 1]


def test_cache_sync_emits_an_empty_fast_forward_batch_for_revision_only_change(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'source.sqlite3'
    state_directory = tmp_path / 'state'
    _database(database_path, 7)
    posted = []

    def post_batch(key: str, content: bytes) -> Mapping[str, Any]:
        posted.append((key, json.loads(content)))
        return {'status': 'applied'}

    def source_builder(_connection: sqlite3.Connection) -> Mapping[str, Any]:
        return _source(1)

    sync_dashboard_cache_once(
        database_path=database_path,
        state_directory=state_directory,
        post_batch=post_batch,
        source_builder=source_builder,
    )
    posted.clear()
    with sqlite3.connect(database_path) as connection:
        connection.execute('UPDATE dashboard_source_state SET revision=8')

    result = sync_dashboard_cache_once(
        database_path=database_path,
        state_directory=state_directory,
        post_batch=post_batch,
        source_builder=source_builder,
    )

    assert result.synced is True
    assert result.batch_count == 1
    assert result.match_count == 0
    assert posted[0][1]['sourceRevision'] == 8
    assert posted[0][1]['matches'] == []
    assert posted[0][1]['reset'] is False
    assert posted[0][1]['publish'] is True
