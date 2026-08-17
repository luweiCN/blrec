import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlsplit

import psycopg
import pytest
from blrec_dashboard_publisher.snapshot import (
    _allows_public_replay,
    _season_for,
    build_dashboard_api_source,
    build_dashboard_asset_source,
    build_dashboard_runtime_source,
    build_dashboard_snapshot,
    export_dashboard_files,
)
from blrec_dashboard_publisher.source_database import connect_source_database

from blrec.bili_upload.database import BiliUploadDatabase
from scripts.migrate_blrec_sqlite_to_postgres import migrate

SHANGHAI = timezone(timedelta(hours=8))


def timestamp(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=SHANGHAI).timestamp())


def test_public_replay_rejects_private_upload_policy() -> None:
    publication = {
        'source_kind': 'upload',
        'visibility_scope': 'public',
        'archive_is_only_self': None,
        'upload_policy_snapshot_json': '{"is_only_self":true}',
    }

    assert _allows_public_replay(publication) is False
    assert (
        _allows_public_replay(
            {**publication, 'upload_policy_snapshot_json': '{"is_only_self":false}'}
        )
        is True
    )
    assert (
        _allows_public_replay(
            {**publication, 'upload_policy_snapshot_json': 'invalid json'}
        )
        is False
    )


async def seed_player(
    database: BiliUploadDatabase, player_id: int, name: str, room_id: int
) -> None:
    await database.execute(
        'INSERT INTO vainglory_players('
        'id,name,origin,created_at,updated_at) VALUES(?,?,\'manual\',1,1)',
        (player_id, name),
    )
    await database.execute(
        'INSERT INTO vainglory_player_rooms('
        'room_id,player_id,created_at,updated_at) VALUES(?,?,1,1)',
        (room_id, player_id),
    )


async def seed_match(
    database: BiliUploadDatabase,
    tmp_path: Path,
    *,
    match_id: int,
    room_id: int,
    started_at: int,
    game_mode: str,
    won: bool,
    hero_id: Optional[int],
    anchor_name: str,
    stats_included: bool = True,
    match_stats_eligible: bool = True,
    team_size: int = 3,
) -> None:
    session_id = match_id
    run_id = 'run:{}'.format(match_id)
    video = tmp_path / 'match-{}.mp4'.format(match_id)
    video.write_bytes(b'video')
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at,title,anchor_name) '
        "VALUES(?,?,?,'closed',?,'样本录播',?)",
        (session_id, room_id, 'session:{}'.format(session_id), started_at, anchor_name),
    )
    await database.execute(
        'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
        "VALUES(?,?,'finished',?,?)",
        (run_id, session_id, started_at, started_at + 1),
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,record_start_time,'
        'artifact_state,created_at,updated_at) '
        "VALUES(?,?,?,?,?,?,'ready',?,?)",
        (
            match_id,
            session_id,
            run_id,
            1,
            str(video),
            started_at,
            started_at,
            started_at,
        ),
    )
    await database.execute(
        'INSERT INTO vainglory_scan_jobs('
        'session_id,state,progress,algorithm_version,match_count,error,'
        'requested_at,started_at,completed_at,updated_at,stats_included) '
        "VALUES(?,'ready',1,13,1,NULL,?,?,?, ?,?)",
        (
            session_id,
            started_at,
            started_at,
            started_at + 1,
            started_at + 1,
            1 if stats_included else 0,
        ),
    )
    await database.execute(
        'INSERT INTO vainglory_matches('
        'id,session_id,result_part_id,result_at_ms,duration_seconds,'
        'result_text,end_reason,left_color,right_color,winner_side,confidence,'
        'created_at,game_mode,team_size,started_at_ms,recorded_player_side,'
        'recorded_player_slot,recorded_player_confidence,'
        'recorded_player_detection_version,stats_eligible,'
        'stats_exclusion_reason) '
        "VALUES(?,?,?,900000,900,'胜利','normal','teal','orange',?,1,?,?,?,?,"
        "'left',1,1,1,?,?)",
        (
            match_id,
            session_id,
            match_id,
            'left' if won else 'right',
            started_at + 1,
            game_mode,
            team_size,
            0,
            1 if match_stats_eligible else 0,
            None if match_stats_eligible else 'bot',
        ),
    )
    await database.execute(
        'INSERT INTO vainglory_match_players('
        'match_id,side,slot,player_name,normalized_name,hero_id,'
        'kills,deaths,assists,economy,confidence) '
        "VALUES(?,'left',1,'OCR 游戏名','ocr游戏名',?,1,1,1,10000,1)",
        (match_id, hero_id),
    )


async def seed_lineup(
    database: BiliUploadDatabase,
    match_id: int,
    players: list[tuple[str, int, str, int, int, int, int, int, int]],
) -> None:
    for (
        side,
        slot,
        name,
        hero_id,
        kills,
        deaths,
        assists,
        economy,
        last_hits,
    ) in players:
        await database.execute(
            'INSERT OR REPLACE INTO vainglory_match_players('
            'match_id,side,slot,player_name,normalized_name,hero_id,'
            'kills,deaths,assists,economy,confidence,last_hits) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,1,?)',
            (
                match_id,
                side,
                slot,
                name,
                name.casefold(),
                hero_id,
                kills,
                deaths,
                assists,
                economy,
                last_hits,
            ),
        )


async def seed_publication(
    database: BiliUploadDatabase, *, match_id: int, bvid: str, page: int, public: bool
) -> None:
    await database.execute(
        'INSERT OR IGNORE INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
        "state,created_at,updated_at) VALUES(1,1,'测试账号',X'01',1,'test','active',1,1)"
    )
    await database.execute(
        'INSERT INTO vainglory_archive_imports('
        'id,account_id,aid,bvid,title,published_at,session_id,state,progress,'
        'page_count,completed_page_count,error,created_at,updated_at) '
        "VALUES(?,1,?,?,?,1,?,'ready',1,1,1,NULL,1,1)",
        (match_id, 1000 + match_id, bvid, '测试稿件', match_id),
    )
    await database.execute(
        'INSERT INTO vainglory_archive_parts('
        'id,import_id,page,cid,title,duration_seconds,recording_part_id,'
        'state,progress,error,created_at,updated_at) '
        "VALUES(?,?,?,?,?,1800,?,'ready',1,NULL,1,1)",
        (1000 + match_id, match_id, page, 2000 + match_id, '测试分段', match_id),
    )
    await database.execute(
        'INSERT INTO vainglory_publications('
        'id,account_id,session_id,aid,bvid,source_kind,payload_hash,'
        'description_block,state,description_state,pin_state,attempt_count,'
        'next_attempt_at,created_at,updated_at,needs_refresh,chapter_state,'
        'public_visible_at,visibility_scope) '
        "VALUES(?,1,?,?,?,'archive',?,'测试','confirmed','confirmed','confirmed',"
        "0,0,1,1,0,'confirmed',?,?)",
        (
            match_id,
            match_id,
            1000 + match_id,
            bvid,
            'a' * 64,
            1 if public else None,
            'public' if public else 'unknown',
        ),
    )


@pytest.mark.asyncio
async def test_dashboard_api_source_tracks_active_bound_live_rooms(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_player(database, 10, '主播', 100)
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,live_start_time,state,started_at,'
            'title,source_kind) '
            "VALUES(1,100,'100:live',1000,'open',1001,'今晚三排','live')"
        )

        live_source = await database.read(
            lambda connection: build_dashboard_api_source(
                connection, now=datetime(2026, 8, 11, 22, tzinfo=SHANGHAI)
            )
        )

        assert live_source['players'][0]['liveRooms'] == [
            {'roomId': 100, 'title': '今晚三排', 'startedAt': '1970-01-01T00:16:40Z'}
        ]

        await database.execute(
            "UPDATE recording_sessions SET live_end_time=1100,state='closed',"
            'ended_at=1100 WHERE id=1'
        )
        offline_source = await database.read(
            lambda connection: build_dashboard_api_source(
                connection, now=datetime(2026, 8, 11, 22, tzinfo=SHANGHAI)
            )
        )
        assert offline_source['players'][0]['liveRooms'] == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_runtime_and_asset_sources_read_the_core_tables_directly(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_player(database, 10, '主播', 100)
        await seed_match(
            database,
            tmp_path,
            match_id=1,
            room_id=100,
            started_at=timestamp(2026, 8, 1),
            game_mode='3v3',
            won=True,
            hero_id=None,
            anchor_name='主播',
        )
        await database.execute(
            "UPDATE vainglory_matches SET result_frame_path='1/result.png' WHERE id=1"
        )
        now = datetime(2026, 8, 3, 10, 30, tzinfo=SHANGHAI)

        runtime = await database.read(
            lambda connection: build_dashboard_runtime_source(connection, now=now)
        )
        assets = await database.read(
            lambda connection: build_dashboard_asset_source(connection, now=now)
        )

        assert runtime['snapshot']['sourceMatchCount'] == 1
        assert runtime['snapshot']['matches'] == []
        assert runtime['matches'][0]['id'] == 1
        assert assets['matches'] == [{'id': 1, 'resultFramePath': '1/result.png'}]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_postgres_source_matches_the_sqlite_snapshot(tmp_path: Path) -> None:
    database_url = os.environ.get('DASHBOARD_PUBLISHER_TEST_POSTGRES_URL', '').strip()
    if not database_url:
        pytest.skip('DASHBOARD_PUBLISHER_TEST_POSTGRES_URL is not configured')
    database_name = urlsplit(database_url).path.lstrip('/')
    assert database_name == 'blrec_dashboard_publisher_test'
    source_path = tmp_path / 'blrec.sqlite3'
    database = BiliUploadDatabase(str(source_path))
    await database.open()
    try:
        await seed_player(database, 10, '主播', 100)
        await seed_match(
            database,
            tmp_path,
            match_id=1,
            room_id=100,
            started_at=timestamp(2026, 8, 1),
            game_mode='3v3',
            won=True,
            hero_id=None,
            anchor_name='主播',
        )
        now = datetime(2026, 8, 3, 10, 30, tzinfo=SHANGHAI)
        expected = await database.read(
            lambda connection: build_dashboard_snapshot(connection, now=now)
        )
    finally:
        await database.close()

    with psycopg.connect(database_url, autocommit=True) as target:
        target.execute('CREATE SCHEMA core')
    separator = '&' if '?' in database_url else '?'
    core_database_url = '{}{}{}'.format(
        database_url, separator, urlencode({'options': '-csearch_path=core'})
    )
    migrate(
        source_path,
        core_database_url,
        expected_database=database_name,
        expected_schema='core',
        backup_directory=tmp_path / 'backups',
    )
    connection = connect_source_database(core_database_url)
    try:
        actual = build_dashboard_snapshot(connection, now=now)
    finally:
        connection.close()

    assert actual == expected


def test_seasons_follow_the_original_game_calendar() -> None:
    cases = (
        ((2026, 2, 28), '2025-winter', (2025, 12, 1), (2026, 3, 1)),
        ((2026, 3, 1), '2026-spring', (2026, 3, 1), (2026, 6, 1)),
        ((2026, 5, 31), '2026-spring', (2026, 3, 1), (2026, 6, 1)),
        ((2026, 6, 1), '2026-summer', (2026, 6, 1), (2026, 9, 1)),
        ((2026, 8, 31), '2026-summer', (2026, 6, 1), (2026, 9, 1)),
        ((2026, 9, 1), '2026-autumn', (2026, 9, 1), (2026, 12, 1)),
        ((2026, 11, 30), '2026-autumn', (2026, 9, 1), (2026, 12, 1)),
        ((2026, 12, 1), '2026-winter', (2026, 12, 1), (2027, 3, 1)),
    )

    for value, expected_key, expected_start, expected_end in cases:
        season = _season_for(datetime(*value, tzinfo=SHANGHAI))

        assert season.key == expected_key
        assert (
            season.starts_at.year,
            season.starts_at.month,
            season.starts_at.day,
        ) == expected_start
        assert (season.ends_at.year, season.ends_at.month, season.ends_at.day) == (
            expected_end
        )


@pytest.mark.asyncio
async def test_snapshot_uses_stable_players_and_beijing_seasons(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO vainglory_heroes('
            'id,fingerprint,thumbnail_png,label,created_at,updated_at) '
            "VALUES(1,'0000000000000001',X'01','Caine',1,1),"
            "(2,'0000000000000002',X'02','Vox',1,1)"
        )
        await seed_player(database, 10, '游戏昵称', 100)
        await seed_player(database, 20, '另一名玩家', 200)
        await seed_match(
            database,
            tmp_path,
            match_id=1,
            room_id=100,
            started_at=timestamp(2026, 5, 31, 23),
            game_mode='3v3',
            won=True,
            hero_id=1,
            anchor_name='旧直播名',
        )
        await seed_match(
            database,
            tmp_path,
            match_id=2,
            room_id=100,
            started_at=timestamp(2026, 6, 1),
            game_mode='3v3',
            won=True,
            hero_id=1,
            anchor_name='直播名称',
        )
        await seed_match(
            database,
            tmp_path,
            match_id=3,
            room_id=100,
            started_at=timestamp(2026, 7, 1),
            game_mode='aram',
            won=False,
            hero_id=2,
            anchor_name='直播名称',
        )
        await database.execute(
            'UPDATE vainglory_match_players SET economy=NULL WHERE match_id=3'
        )
        await seed_match(
            database,
            tmp_path,
            match_id=4,
            room_id=200,
            started_at=timestamp(2026, 8, 1),
            game_mode='3v3',
            won=True,
            hero_id=1,
            anchor_name='另一名主播',
        )
        await database.execute(
            'INSERT INTO vainglory_player_sessions('
            'session_id,player_id,created_at,updated_at) VALUES(4,10,1,1)'
        )
        await seed_match(
            database,
            tmp_path,
            match_id=5,
            room_id=200,
            started_at=timestamp(2026, 8, 2),
            game_mode='unknown',
            won=True,
            hero_id=1,
            anchor_name='另一名主播',
        )
        await seed_match(
            database,
            tmp_path,
            match_id=6,
            room_id=200,
            started_at=timestamp(2026, 8, 3),
            game_mode='3v3',
            won=True,
            hero_id=1,
            anchor_name='另一名主播',
            stats_included=False,
        )
        await seed_match(
            database,
            tmp_path,
            match_id=7,
            room_id=100,
            started_at=timestamp(2026, 8, 4),
            game_mode='3v3',
            won=False,
            hero_id=1,
            anchor_name='直播名称',
            match_stats_eligible=False,
        )

        snapshot = await database.read(
            lambda connection: build_dashboard_snapshot(
                connection, now=datetime(2026, 8, 3, 10, 30, tzinfo=SHANGHAI)
            )
        )

        assert snapshot['schemaVersion'] == 3
        assert snapshot['currentSeasonKey'] == '2026-summer'
        assert [season['key'] for season in snapshot['seasons']] == [
            '2026-summer',
            '2026-spring',
            'all-time',
        ]
        summer = snapshot['standings']['2026-summer']
        assert [player['name'] for player in summer['players']] == [
            '游戏昵称',
            '另一名玩家',
        ]
        first = summer['players'][0]
        assert first['roomLabel'] == '直播间 100'
        assert first['roomIds'] == [100]
        assert first['aliases'] == ['旧直播名', '直播名称']
        assert 'OCR 游戏名' not in first['aliases']
        assert first['modes']['all']['matches'] == 2
        assert first['modes']['all']['wins'] == 1
        assert first['modes']['all']['topHero'] == 'Caine'
        assert first['modes']['all']['form'] == ['W', 'L']
        assert first['modes']['3v3']['matches'] == 1
        assert isinstance(first['modes']['3v3']['ratingScore'], (int, float))
        assert first['modes']['3v3']['provisional'] is True
        forecast = first['modes']['3v3']['ratingForecast']
        assert forecast['nextWinScore'] > first['modes']['3v3']['ratingScore']
        assert forecast['nextLossScore'] < first['modes']['3v3']['ratingScore']
        assert forecast['nextDivision']['targetDisplayScore'] > (
            first['modes']['3v3']['ratingScore'] * 3
        )
        assert forecast['nextDivision']['allWinMatches'] > 0
        assert first['modes']['5v5']['ratingForecast'] is None
        assert first['modes']['5v5']['ratingScore'] is None
        assert first['modes']['brawl']['matches'] == 1
        assert first['modes']['5v5']['matches'] == 0
        assert [
            (usage['name'], usage['matches'], usage['wins'])
            for usage in first['heroPool']
        ] == [('Caine', 1, 1), ('Vox', 1, 0)]
        assert first['heroPool'][0]['stats'] == {
            'kdaMatches': 1,
            'kills': 1,
            'deaths': 1,
            'assists': 1,
            'economyMatches': 1,
            'economy': 10000,
            'economyDurationSeconds': 900,
        }
        assert first['heroPool'][1]['stats']['economyMatches'] == 0
        assert [usage['name'] for usage in first['heroPools']['3v3']] == ['Caine']
        assert [usage['name'] for usage in first['heroPools']['brawl']] == ['Vox']
        assert first['heroPools']['5v5'] == []
        assert [(hero['name'], hero['modes']['all']) for hero in summer['heroes']] == [
            ('Caine', {'matches': 2, 'wins': 2, 'players': 2}),
            ('Vox', {'matches': 1, 'wins': 0, 'players': 1}),
        ]
        assert snapshot['standings']['2026-spring']['players'][0]['name'] == (
            '游戏昵称'
        )
        assert snapshot['sourceLastMatchId'] == 4
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_snapshot_excludes_incomplete_or_conflicting_matches(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_player(database, 10, '玩家', 100)
        for match_id, game_mode, team_size in (
            (1, '3v3', 3),
            (2, '5v5', 5),
            (3, 'aram', 3),
            (4, '5v5', 3),
            (5, '3v3', 5),
            (6, '5v5', 5),
        ):
            await seed_match(
                database,
                tmp_path,
                match_id=match_id,
                room_id=100,
                started_at=timestamp(2026, 8, match_id),
                game_mode=game_mode,
                won=True,
                hero_id=None,
                anchor_name='主播',
                team_size=team_size,
            )
        await database.execute(
            'UPDATE vainglory_matches SET recorded_player_side=NULL,'
            'recorded_player_slot=NULL WHERE id=6'
        )

        snapshot = await database.read(
            lambda connection: build_dashboard_snapshot(
                connection, now=datetime(2026, 8, 8, 10, 30, tzinfo=SHANGHAI)
            )
        )

        player = snapshot['standings']['2026-summer']['players'][0]
        assert snapshot['sourceMatchCount'] == 3
        assert snapshot['sourceLastMatchId'] == 3
        assert player['modes']['all']['matches'] == 3
        assert player['modes']['3v3']['matches'] == 1
        assert player['modes']['5v5']['matches'] == 1
        assert player['modes']['brawl']['matches'] == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_snapshot_exports_matches_by_live_time_and_hides_private_replays(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO vainglory_heroes('
            'id,fingerprint,thumbnail_png,label,created_at,updated_at) VALUES'
            "(1,'0000000000000001',X'01','Caine',1,1),"
            "(2,'0000000000000002',X'02','Ardan',1,1),"
            "(3,'0000000000000003',X'03','Gwen',1,1),"
            "(4,'0000000000000004',X'04','Koshka',1,1),"
            "(5,'0000000000000005',X'05','Vox',1,1),"
            "(6,'0000000000000006',X'06','Lance',1,1)"
        )
        await seed_player(database, 10, '卢伟', 100)

        await seed_match(
            database,
            tmp_path,
            match_id=1,
            room_id=100,
            started_at=timestamp(2026, 8, 10, 20),
            game_mode='3v3',
            won=True,
            hero_id=1,
            anchor_name='直播名称',
        )
        await database.execute(
            'UPDATE vainglory_matches SET started_at_ms=120000,'
            'duration_seconds=780,left_kills=14,right_kills=3,'
            'left_economy=40900,right_economy=33000 WHERE id=1'
        )
        await seed_lineup(
            database,
            1,
            [
                ('left', 1, '毒奶的钢门', 1, 8, 0, 4, 16500, 900),
                ('left', 2, '不是小白', 2, 5, 2, 9, 13600, 175),
                ('left', 3, '缸一', 3, 1, 1, 10, 10700, 634),
                ('right', 1, '猪国栋', 4, 1, 7, 2, 11100, 25),
                ('right', 2, 'dove', 5, 0, 3, 3, 7700, 62),
                ('right', 3, '不要输给小白', 6, 2, 4, 0, 14100, 301),
            ],
        )
        await seed_publication(
            database, match_id=1, bvid='BV1public001', page=2, public=True
        )

        await seed_match(
            database,
            tmp_path,
            match_id=2,
            room_id=100,
            started_at=timestamp(2026, 8, 11, 20),
            game_mode='3v3',
            won=False,
            hero_id=1,
            anchor_name='直播名称',
        )
        await seed_lineup(
            database,
            2,
            [
                ('left', 1, '毒奶的钢门', 1, 1, 4, 2, 9000, 100),
                ('left', 2, '不是小白', 2, 2, 4, 1, 8500, 80),
                ('left', 3, '缸一', 3, 0, 5, 3, 8000, 70),
                ('right', 1, '猪国栋', 4, 6, 1, 5, 15000, 500),
                ('right', 2, 'dove', 5, 4, 1, 6, 14500, 400),
                ('right', 3, '不要输给小白', 6, 3, 1, 7, 14000, 300),
            ],
        )
        await seed_publication(
            database, match_id=2, bvid='BV1private01', page=1, public=True
        )
        await database.execute(
            'UPDATE vainglory_archive_imports SET is_only_self=1 WHERE id=2'
        )

        snapshot = await database.read(
            lambda connection: build_dashboard_snapshot(
                connection, now=datetime(2026, 8, 11, 22, tzinfo=SHANGHAI)
            )
        )

        matches = snapshot['matches']
        assert [match['id'] for match in matches] == [2, 1]
        assert matches[0]['playedAt'] > matches[1]['playedAt']
        assert 'replay' not in matches[0]
        assert matches[1]['replay'] == {
            'kind': 'match',
            'url': 'https://www.bilibili.com/video/BV1public001?p=2&t=120',
        }
        assert matches[1]['playerId'] == 10
        assert matches[1]['result'] == 'W'
        assert matches[1]['streamTitle'] == '样本录播'
        assert matches[1]['ally']['role'] == 'ally'
        assert matches[1]['enemy']['role'] == 'enemy'
        assert matches[1]['ally']['players'][0]['slot'] == 1
        assert [player['heroName'] for player in matches[1]['ally']['players']] == [
            'Caine',
            'Ardan',
            'Gwen',
        ]
        assert [player['heroName'] for player in matches[1]['enemy']['players']] == [
            'Koshka',
            'Vox',
            'Lance',
        ]
        assert matches[1]['ally']['economy'] == 40900
        assert matches[1]['enemy']['economy'] == 33000
        assert matches[1]['ally']['players'][0]['lastHits'] is None

        await database.execute(
            "UPDATE vainglory_matches SET result_frame_path='session/result.png' "
            'WHERE id=1'
        )
        api_source = await database.read(
            lambda connection: build_dashboard_api_source(
                connection, now=datetime(2026, 8, 11, 22, tzinfo=SHANGHAI)
            )
        )
        api_match = next(value for value in api_source['matches'] if value['id'] == 1)
        assert api_match['resultFramePath'] == 'session/result.png'
        assert api_source['players'][0]['name'] == '卢伟'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_snapshot_calculates_best_and_worst_hero_synergies(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO vainglory_heroes('
            'id,fingerprint,thumbnail_png,label,created_at,updated_at) VALUES'
            "(1,'0000000000000001',X'01','Caine',1,1),"
            "(2,'0000000000000002',X'02','Ardan',1,1),"
            "(3,'0000000000000003',X'03','Vox',1,1),"
            "(4,'0000000000000004',X'04','Gwen',1,1),"
            "(5,'0000000000000005',X'05','Koshka',1,1),"
            "(6,'0000000000000006',X'06','Lance',1,1)"
        )
        await seed_player(database, 10, '主播', 100)
        for match_id in range(1, 11):
            won = match_id <= 5
            await seed_match(
                database,
                tmp_path,
                match_id=match_id,
                room_id=100,
                started_at=timestamp(2026, 8, match_id),
                game_mode='3v3',
                won=won,
                hero_id=1,
                anchor_name='主播',
            )
            await seed_lineup(
                database,
                match_id,
                [
                    ('left', 1, '主播', 1, 1, 1, 1, 10000, 100),
                    ('left', 2, '搭档', 2 if won else 3, 1, 1, 1, 10000, 100),
                    ('left', 3, '固定搭档', 4, 1, 1, 1, 10000, 100),
                    ('right', 1, '对手甲', 5, 1, 1, 1, 10000, 100),
                    ('right', 2, '对手乙', 6, 1, 1, 1, 10000, 100),
                    ('right', 3, '对手丙', 2 if not won else 3, 1, 1, 1, 10000, 100),
                ],
            )

        snapshot = await database.read(
            lambda connection: build_dashboard_snapshot(
                connection, now=datetime(2026, 8, 11, 22, tzinfo=SHANGHAI)
            )
        )

        heroes = snapshot['standings']['2026-summer']['environmentHeroes']
        caine = next(hero for hero in heroes if hero['name'] == 'Caine')
        best = caine['synergies']['3v3']['best'][0]
        assert (best['name'], best['matches'], best['wins']) == ('Ardan', 5, 5)
        assert best['delta'] > 0
        worst = caine['synergies']['3v3']['worst'][0]
        assert (worst['name'], worst['matches'], worst['wins']) == ('Vox', 5, 0)
        assert worst['delta'] < 0
        assert {item['name'] for item in caine['synergies']['3v3']['best']}.isdisjoint(
            item['name'] for item in caine['synergies']['3v3']['worst']
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_historical_backfill_recalculates_every_later_season(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_player(database, 10, '稳定强者', 100)
        await seed_player(database, 20, '对照玩家', 200)
        match_id = 1

        async def add_results(room_id: int, started_at: int, results: list) -> None:
            nonlocal match_id
            for won in results:
                await seed_match(
                    database,
                    tmp_path,
                    match_id=match_id,
                    room_id=room_id,
                    started_at=started_at + match_id,
                    game_mode='3v3',
                    won=won,
                    hero_id=None,
                    anchor_name='主播',
                )
                match_id += 1

        await add_results(100, timestamp(2026, 7, 1), [True, False])
        await add_results(200, timestamp(2026, 7, 1), [True, False])

        def rating(snapshot: object, season: str, player_id: int) -> int:
            players = snapshot['standings'][season]['players']
            player = next(value for value in players if value['id'] == player_id)
            return player['modes']['3v3']['ratingScore']

        now = datetime(2026, 8, 3, 10, 30, tzinfo=SHANGHAI)
        without_history = await database.read(
            lambda connection: build_dashboard_snapshot(connection, now=now)
        )
        initial_strong = rating(without_history, '2026-summer', 10)
        initial_control = rating(without_history, '2026-summer', 20)
        assert initial_strong == initial_control

        await add_results(100, timestamp(2026, 3, 1), [True] * 5)
        await add_results(200, timestamp(2026, 3, 1), [False] * 5)
        with_spring = await database.read(
            lambda connection: build_dashboard_snapshot(connection, now=now)
        )
        spring_strong = rating(with_spring, '2026-summer', 10)
        spring_control = rating(with_spring, '2026-summer', 20)
        assert spring_strong > spring_control
        assert spring_strong != initial_strong
        assert spring_control != initial_control

        await add_results(100, timestamp(2025, 10, 1), [True] * 5)
        await add_results(200, timestamp(2025, 10, 1), [False] * 5)
        with_autumn = await database.read(
            lambda connection: build_dashboard_snapshot(connection, now=now)
        )
        assert rating(with_autumn, '2026-spring', 10) > rating(
            with_autumn, '2026-spring', 20
        )
        assert rating(with_autumn, '2026-summer', 10) > rating(
            with_autumn, '2026-summer', 20
        )
        assert rating(with_autumn, '2026-summer', 10) != spring_strong
        assert with_autumn['ratingModel'] == {'version': 6}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_export_writes_an_immutable_snapshot_then_manifest(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_player(database, 10, '玩家', 100)
        await seed_match(
            database,
            tmp_path,
            match_id=1,
            room_id=100,
            started_at=timestamp(2026, 8, 1),
            game_mode='3v3',
            won=True,
            hero_id=None,
            anchor_name='主播',
        )
    finally:
        await database.close()

    output = tmp_path / 'public-data'
    result = export_dashboard_files(
        tmp_path / 'blrec.sqlite3',
        output,
        now=datetime(2026, 8, 3, 10, 30, tzinfo=SHANGHAI),
    )

    assert result.manifest_path == output / 'manifest.json'
    assert result.snapshot_path.is_file()
    assert result.snapshot_path.parent == output / 'snapshots'
    manifest = result.manifest
    assert manifest['schemaVersion'] == 1
    assert manifest['publicationDate'] == '2026-08-03'
    assert manifest['snapshotPath'] == 'snapshots/{}.json'.format(
        manifest['snapshotId']
    )
    assert manifest['sha256'] == result.sha256
    assert len(manifest['contentRevision']) == 64

    later = export_dashboard_files(
        tmp_path / 'blrec.sqlite3',
        tmp_path / 'later-public-data',
        now=datetime(2026, 8, 3, 10, 45, tzinfo=SHANGHAI),
    )
    assert later.manifest['snapshotId'] != manifest['snapshotId']
    assert later.manifest['contentRevision'] == manifest['contentRevision']
