from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.vainglory.dashboard_snapshot import (
    build_dashboard_snapshot,
    export_dashboard_files,
)

SHANGHAI = timezone(timedelta(hours=8))


def timestamp(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=SHANGHAI).timestamp())


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
            started_at=timestamp(2026, 4, 30, 23),
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
            started_at=timestamp(2026, 5, 1),
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
            started_at=timestamp(2026, 6, 1),
            game_mode='aram',
            won=False,
            hero_id=2,
            anchor_name='直播名称',
        )
        await seed_match(
            database,
            tmp_path,
            match_id=4,
            room_id=200,
            started_at=timestamp(2026, 7, 1),
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
            started_at=timestamp(2026, 7, 2),
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
            started_at=timestamp(2026, 7, 3),
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
            started_at=timestamp(2026, 7, 4),
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

        assert snapshot['schemaVersion'] == 2
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
        assert first['aliases'] == ['旧直播名', '直播名称']
        assert 'OCR 游戏名' not in first['aliases']
        assert first['modes']['all']['matches'] == 2
        assert first['modes']['all']['wins'] == 1
        assert first['modes']['all']['topHero'] == 'Caine'
        assert first['modes']['all']['form'] == ['W', 'L']
        assert first['modes']['3v3']['matches'] == 1
        assert isinstance(first['modes']['3v3']['ratingScore'], int)
        assert first['modes']['3v3']['provisional'] is True
        assert first['modes']['5v5']['ratingScore'] is None
        assert first['modes']['brawl']['matches'] == 1
        assert first['modes']['5v5']['matches'] == 0
        assert first['heroPool'] == [
            {'name': 'Caine', 'matches': 1, 'wins': 1},
            {'name': 'Vox', 'matches': 1, 'wins': 0},
        ]
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
async def test_snapshot_excludes_mode_and_team_size_conflicts(tmp_path: Path) -> None:
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
        assert spring_strong > initial_strong
        assert spring_control < initial_control

        await add_results(100, timestamp(2025, 10, 1), [True] * 5)
        await add_results(200, timestamp(2025, 10, 1), [False] * 5)
        with_autumn = await database.read(
            lambda connection: build_dashboard_snapshot(connection, now=now)
        )
        assert rating(with_autumn, '2026-spring', 10) > rating(
            with_spring, '2026-spring', 10
        )
        assert rating(with_autumn, '2026-summer', 10) > spring_strong
        assert with_autumn['ratingModel'] == {
            'carryoverRate': 0.25,
            'credibleLevel': 0.9,
            'minimumOutcomeDelta': 1,
            'priorMatches': 20,
            'provisionalMatches': 5,
            'version': 2,
        }
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
