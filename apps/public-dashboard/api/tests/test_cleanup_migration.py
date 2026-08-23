from __future__ import annotations

from pathlib import Path

import pytest
from blrec_dashboard_api import database as database_module
from blrec_dashboard_api.database import connect_database, initialize_database

OBSOLETE_CACHE_TABLES = (
    'dashboard_cache_match_search',
    'dashboard_cache_match_heroes',
    'dashboard_cache_matches',
    'dashboard_cache_players',
    'dashboard_cache_state',
    'dashboard_cache_generations',
    'match_search',
    'match_participants',
    'rating_events',
    'match_teams',
    'matches',
    'player_live_rooms',
    'player_aliases',
    'player_rooms',
    'players',
    'removed_matches',
    'ingestion_batches',
    'dashboard_audience_state',
    'dashboard_state',
    'dashboard_trend_publications',
    'dashboard_publication_standings',
    'dashboard_publications',
)


def _seed_version_ten(database_path: Path) -> None:
    connection = connect_database(database_path)
    try:
        connection.execute(
            'INSERT INTO dashboard_cache_generations('
            'source_revision,audience,dashboard_payload,live_rooms_payload,published_at'
            ") VALUES(1,'public',?,?,1)",
            (b'{}', b'{}'),
        )
        connection.execute(
            'INSERT INTO dashboard_cache_players('
            'source_revision,audience,player_id,player_json'
            ") VALUES(1,'public',1,'{}')"
        )
        connection.execute(
            'INSERT INTO dashboard_cache_matches('
            'source_revision,audience,match_id,player_id,season_key,mode,'
            'played_at_epoch,result,duration_seconds,has_replay,match_json,'
            'ratings_json) VALUES('
            "1,'public',1,1,'2026-summer','3v3',1,'W',60,0,'{}','{}')"
        )
        connection.execute(
            'INSERT INTO dashboard_cache_match_search('
            'source_revision,audience,match_id,form_index,normalized,pinyin,initials'
            ") VALUES(1,'public',1,0,'a','a','a')"
        )
        connection.execute(
            'INSERT INTO dashboard_cache_match_heroes('
            'source_revision,audience,match_id,hero_name'
            ") VALUES(1,'public',1,'hero')"
        )
        connection.execute(
            "INSERT INTO dashboard_cache_state(audience,source_revision,published_at) "
            "VALUES('public',1,1)"
        )
        connection.execute(
            'INSERT INTO players('
            'player_id,name,initial,room_label,avatar_url,updated_at'
            ") VALUES(1,'player','p','room',NULL,1)"
        )
        connection.execute(
            'INSERT INTO matches('
            'source_match_id,revision_sha256,player_id,season_key,mode,played_at,'
            'played_at_epoch,duration_seconds,result,stream_title,replay_kind,'
            'replay_url,result_image_url,result_image_width,result_image_height,'
            'created_at,updated_at) VALUES('
            "1,? ,1,'2026-summer','3v3','2026-06-01T00:00:00Z',1,60,'W',"
            "'stream',NULL,NULL,NULL,NULL,NULL,1,1)",
            ('a' * 64,),
        )
        connection.execute(
            'INSERT INTO ingestion_batches('
            'idempotency_key,payload_sha256,source_last_match_id,match_count,'
            'removed_match_count,applied_at) VALUES(?,?,?,?,?,?)',
            ('old-cache', 'b' * 64, 1, 1, 0, 1),
        )
        connection.execute(
            'INSERT INTO dashboard_audience_state('
            'audience,source_revision,dashboard_payload,live_rooms_payload,published_at'
            ") VALUES('public',1,?,?,1)",
            (b'{}', b'{}'),
        )
        connection.execute(
            'INSERT INTO dashboard_publications('
            'publication_date,source_revision,snapshot_id,generated_at,'
            'source_last_match_id,updated_at) VALUES('
            "'2026-06-01',1,'snapshot','2026-06-01T00:00:00Z',1,1)"
        )
        connection.execute(
            'INSERT INTO match_assets('
            'source_match_id,image_url,image_width,image_height,image_sha256,updated_at'
            ") VALUES(1,'https://example.com/1.webp',100,50,?,1)",
            ('c' * 64,),
        )
        connection.execute(
            'INSERT INTO asset_batches('
            'idempotency_key,payload_sha256,image_count,removed_match_count,applied_at'
            ') VALUES(?,?,?,?,?)',
            ('asset-batch', 'd' * 64, 1, 0, 1),
        )
        connection.execute(
            'INSERT INTO replay_visibility_checks('
            'bvid,state,checked_at,expires_at,requested_at,claimed_at,'
            'attempt_count,next_attempt_at,last_error,updated_at) '
            "VALUES('BV1test00001','public',1,2,1,NULL,1,2,NULL,1)"
        )
        connection.commit()
    finally:
        connection.close()


def test_schema_eleven_clears_obsolete_cache_and_preserves_auxiliary_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / 'dashboard.sqlite3'
    monkeypatch.setattr(database_module, 'LATEST_SCHEMA_VERSION', 10)
    initialize_database(database_path)
    _seed_version_ten(database_path)

    monkeypatch.setattr(database_module, 'LATEST_SCHEMA_VERSION', 11)
    initialize_database(database_path)

    connection = connect_database(database_path)
    try:
        version = int(
            connection.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[
                0
            ]
        )
        obsolete_counts = {
            table: int(
                connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            )
            for table in OBSOLETE_CACHE_TABLES
        }
        assets = int(
            connection.execute('SELECT COUNT(*) FROM match_assets').fetchone()[0]
        )
        batches = int(
            connection.execute('SELECT COUNT(*) FROM asset_batches').fetchone()[0]
        )
        checks = int(
            connection.execute(
                'SELECT COUNT(*) FROM replay_visibility_checks'
            ).fetchone()[0]
        )
    finally:
        connection.close()

    assert version == 11
    assert set(obsolete_counts.values()) == {0}
    assert (assets, batches, checks) == (1, 1, 1)
