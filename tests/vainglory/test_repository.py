from dataclasses import replace
from pathlib import Path

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.vainglory.analyzer import AnalyzedHero, AnalyzedMatch, ScannedPart
from blrec.vainglory.hero_recognition import HeroReference
from blrec.vainglory.ocr import OcrPlayer, PlayerStats, ResultHeader, ResultOcr
from blrec.vainglory.repository import VaingloryNotFound, VaingloryRepository
from blrec.vainglory.vision import RecordedPlayer, ResultLayout


async def seed_session(
    database: BiliUploadDatabase,
    path: Path,
    *,
    session_id: int = 1,
    state: str = 'closed',
) -> None:
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at,title,anchor_name) '
        'VALUES(?,?,?,?,?,?,?)',
        (
            session_id,
            100,
            'session:{}'.format(session_id),
            state,
            1_000,
            '样本录播',
            '样本主播',
        ),
    )
    await database.execute(
        'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
        "VALUES(?,?, 'finished',1,2)",
        ('run:{}'.format(session_id), session_id),
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,record_start_time,'
        'artifact_state,created_at,updated_at) '
        "VALUES(?,?,?,?,?,1,'ready',1,1)",
        (session_id, session_id, 'run:{}'.format(session_id), 1, str(path)),
    )


async def seed_part(
    database: BiliUploadDatabase,
    path: Path,
    *,
    session_id: int,
    part_id: int,
    part_index: int,
) -> None:
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,record_start_time,'
        'artifact_state,created_at,updated_at) '
        "VALUES(?,?,?,?,?,1,'ready',1,1)",
        (part_id, session_id, 'run:{}'.format(session_id), part_index, str(path)),
    )


def analyzed_match() -> AnalyzedMatch:
    players = []
    for side_index, side in enumerate(('left', 'right')):
        for slot in range(1, 4):
            players.append(
                OcrPlayer(
                    side=side,
                    slot=slot,
                    name='玩家{}{}'.format(side_index, slot),
                    normalized_name='玩家{}{}'.format(side_index, slot),
                    stats=PlayerStats(
                        kills=slot,
                        deaths=4 - slot,
                        assists=slot + 1,
                        economy=10_000 + slot * 100,
                        last_hits=slot * 10,
                    ),
                    confidence=0.9,
                )
            )
    return AnalyzedMatch(
        part_id=1,
        part_index=1,
        result_at_ms=123_500,
        layout=ResultLayout(
            left_color='orange',
            right_color='teal',
            winner_color='teal',
            winner_side='right',
            confidence=1.0,
        ),
        ocr=ResultOcr(
            header=ResultHeader(
                result_text='投降',
                end_reason='surrender',
                duration_seconds=900,
                left_kills=6,
                right_kills=6,
                left_economy=30_600,
                right_economy=30_600,
            ),
            players=tuple(players),
        ),
        heroes=tuple(
            AnalyzedHero(
                side=side,
                slot=slot,
                fingerprint='{:016x}'.format(side_index * 3 + slot),
                thumbnail_png=b'\x89PNG',
                label='Caine',
            )
            for side_index, side in enumerate(('left', 'right'))
            for slot in range(1, 4)
        ),
        confidence=0.95,
        result_frame_png=b'\x89PNG-result-frame',
    )


@pytest.mark.asyncio
async def test_ocr_queue_preserves_observed_candidate_context(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        repository = VaingloryRepository(database, clock=lambda: 100)
        await repository.request_scan(1)
        assert await repository.claim_next() is not None
        await repository.enqueue_ocr(
            1,
            ScannedPart(
                video_duration_ms=1_000,
                candidate_times_ms=(500,),
                candidate_view_contexts=('observed',),
            ),
        )

        claim = await repository.claim_next_ocr()

        assert claim is not None
        assert claim.scanned.candidate_times_ms == (500,)
        assert claim.scanned.candidate_view_contexts == ('observed',)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_claims_persists_and_searches_matches(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        repository = VaingloryRepository(database, clock=lambda: 100)
        await repository.sync_hero_references(
            (HeroReference('Caine', 'a' * 64, b'\xff\xd8reference'),)
        )

        requested = await repository.request_scan(1)
        claim = await repository.claim_next()
        assert requested.state == 'pending'
        assert claim is not None
        assert claim.parts[0].path == str(video)

        await repository.complete_part(1, (analyzed_match(),))
        page = await repository.list_matches(player_name='玩家12', winner_color='teal')

        assert page.total == 1
        assert page.items[0].end_reason == 'surrender'
        assert page.items[0].winner_color == 'teal'
        assert page.items[0].part_index == 1
        assert page.items[0].game_mode == '3v3'
        assert page.items[0].team_size == 3
        assert page.items[0].started_at_ms == 0
        assert page.items[0].title == '样本录播'
        assert page.items[0].source_title == '样本录播'
        assert page.items[0].upload_title == ''
        assert len(page.items[0].players) == 6
        assert page.items[0].players[4].name == '玩家12'
        assert page.items[0].players[4].last_hits == 20
        assert page.items[0].players[0].hero_id is not None
        assert page.items[0].has_result_frame is True
        result_frame_path = await repository.result_frame_path(page.items[0].id)
        assert result_frame_path is not None
        assert result_frame_path.read_bytes() == b'\x89PNG-result-frame'
        assert await database.scalar(
            'SELECT result_frame_path FROM vainglory_matches WHERE id=?',
            (page.items[0].id,),
        ) == ('session-1/part-1-123500.png')
        hero_ids = tuple(value.id for value in await repository.list_heroes())
        lineup_page = await repository.list_matches(
            hero_ids=(hero_ids[0], hero_ids[-1])
        )
        missing_lineup_page = await repository.list_matches(hero_ids=(hero_ids[0], 999))
        assert lineup_page.total == 1
        assert missing_lineup_page.total == 0
        job = await repository.get_job(1)
        assert job is not None
        assert job.state == 'ready'
        assert job.match_count == 1
        result_frame_path.unlink()
        assert (await repository.get_match(page.items[0].id)).has_result_frame is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_rematches_only_missing_heroes_from_saved_results(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        repository = VaingloryRepository(database, clock=lambda: 100)
        await repository.sync_hero_references(
            (HeroReference('Caine', 'a' * 64, b'\xff\xd8reference'),)
        )
        await repository.request_scan(1)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))
        match_id = int(await database.scalar('SELECT id FROM vainglory_matches'))
        known_hero_id = int(
            await database.scalar(
                'SELECT hero_id FROM vainglory_match_players '
                "WHERE match_id=? AND side='left' AND slot=2",
                (match_id,),
            )
        )
        await database.execute(
            "UPDATE vainglory_match_players SET hero_id=NULL "
            "WHERE match_id=? AND side='left' AND slot=1",
            (match_id,),
        )
        await database.execute(
            'UPDATE vainglory_matches SET hero_recognition_version=1 WHERE id=?',
            (match_id,),
        )

        claim = await repository.next_hero_rematch()

        assert claim is not None
        assert claim.match_id == match_id
        assert (
            await repository.complete_hero_rematch(
                match_id,
                (
                    AnalyzedHero(
                        side='left',
                        slot=1,
                        fingerprint='f' * 64,
                        thumbnail_png=b'ignored',
                        label='Caine',
                    ),
                    AnalyzedHero(
                        side='left',
                        slot=2,
                        fingerprint='e' * 64,
                        thumbnail_png=b'ignored',
                        label='unknown',
                    ),
                ),
            )
            == 1
        )
        rows = await database.fetchall(
            'SELECT slot,hero_id FROM vainglory_match_players '
            "WHERE match_id=? AND side='left' ORDER BY slot",
            (match_id,),
        )
        assert int(rows[0]['hero_id']) == known_hero_id
        assert int(rows[1]['hero_id']) == known_hero_id
        assert await repository.next_hero_rematch() is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reanalysis_replaces_result_frame_files(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        repository = VaingloryRepository(database, clock=lambda: 100)

        await repository.request_scan(1)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))
        first_match = (await repository.list_matches()).items[0]
        first_path = await repository.result_frame_path(first_match.id)
        assert first_path is not None
        assert first_path.is_file()

        replacement = replace(analyzed_match(), result_at_ms=456_000)
        await repository.request_scan(1)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (replacement,))
        second_match = (await repository.list_matches()).items[0]
        second_path = await repository.result_frame_path(second_match.id)

        assert second_path is not None
        assert second_path.name == 'part-1-456000.png'
        assert second_path.read_bytes() == b'\x89PNG-result-frame'
        assert not first_path.exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_auto_discovers_and_claims_ready_part_while_recording_is_open(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video, state='open')
        repository = VaingloryRepository(database, clock=lambda: 100)

        assert await repository.discover_ready_parts() == 1
        claim = await repository.claim_next()

        assert claim is not None
        assert claim.session_id == 1
        assert claim.part.id == 1
        current = await repository.get_job(1)
        assert current is not None
        assert current.state == 'analyzing'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_prioritizes_open_recording_over_older_backlog(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        old_video = tmp_path / 'old.mp4'
        live_video = tmp_path / 'live.mp4'
        old_video.write_bytes(b'old')
        live_video.write_bytes(b'live')
        await seed_session(database, old_video, session_id=1)
        await seed_session(database, live_video, session_id=2, state='open')
        repository = VaingloryRepository(database, clock=lambda: 100)

        claim = await repository.claim_next()

        assert claim is not None
        assert claim.session_id == 2
        assert claim.part.id == 2
        assert claim.realtime is True
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_uses_builtin_label_and_canonical_id(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        repository = VaingloryRepository(database, clock=lambda: 100)
        await repository.sync_hero_references(
            (HeroReference('Caine', 'a' * 64, b'\xff\xd8reference'),)
        )
        match = analyzed_match()
        heroes = list(match.heroes)
        heroes[0] = replace(heroes[0], fingerprint='04df9130fe4c0c7a32', label='')
        heroes[3] = replace(heroes[3], fingerprint='04df9130fe4c0c7a33', label='')
        match = replace(match, heroes=tuple(heroes))

        requested = await repository.request_scan(1)
        assert requested.algorithm_version == repository.ALGORITHM_VERSION
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (match,))

        page = await repository.list_matches()
        assert page.items[0].players[0].hero_label == 'Caine'
        assert page.items[0].players[3].hero_label == 'Caine'
        assert page.items[0].players[0].hero_id == page.items[0].players[3].hero_id
        caine_records = [
            hero for hero in await repository.list_heroes() if hero.label == 'Caine'
        ]
        assert len(caine_records) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_keeps_high_resolution_reference_for_sift_match(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        repository = VaingloryRepository(database, clock=lambda: 100)
        reference = HeroReference('Caine', 'a' * 64, b'\xff\xd8high-resolution')
        assert await repository.sync_hero_references((reference,)) == 1
        match = analyzed_match()
        heroes = list(match.heroes)
        heroes[0] = replace(heroes[0], label='Caine', confidence=0.95)
        match = replace(match, heroes=tuple(heroes))

        await repository.request_scan(1)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (match,))

        player = (await repository.list_matches()).items[0].players[0]
        assert player.hero_label == 'Caine'
        assert player.hero_id is not None
        assert await repository.hero_thumbnail(player.hero_id) == reference.image_jpeg
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_does_not_create_unknown_heroes_or_replace_references(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        repository = VaingloryRepository(database, clock=lambda: 100)
        reference = HeroReference('Caine', 'a' * 64, b'\xff\xd8high-resolution')
        await repository.sync_hero_references((reference,))
        heroes = list(analyzed_match().heroes)
        heroes[1] = replace(
            heroes[1],
            label='',
            fingerprint='ffffffffffffffff',
            thumbnail_png=b'\x89PNG-low-resolution',
        )

        await repository.request_scan(1)
        assert await repository.claim_next() is not None
        await repository.complete_part(
            1, (replace(analyzed_match(), heroes=tuple(heroes)),)
        )

        stored = await repository.list_heroes()
        players = (await repository.list_matches()).items[0].players
        assert [(hero.label, hero.fingerprint) for hero in stored] == [
            ('Caine', reference.fingerprint)
        ]
        assert players[0].hero_id == stored[0].id
        assert players[1].hero_id is None
        assert await repository.hero_thumbnail(stored[0].id) == reference.image_jpeg
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM vainglory_heroes WHERE label=''"
            )
            == 0
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_backfills_only_empty_builtin_labels(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO vainglory_heroes('
            'id,fingerprint,thumbnail_png,label,created_at,updated_at) '
            "VALUES(1,'04c3c71e4b83b8585b',X'01','',1,1),"
            "(2,'04cc72548b54d5c6e5',X'01','自定义名称',1,1),"
            "(3,'0000000000000000',X'01','',1,1)"
        )
        repository = VaingloryRepository(database, clock=lambda: 100)

        assert await repository.apply_builtin_hero_labels() == 1
        labels = {
            int(row['id']): str(row['label'])
            for row in await database.fetchall(
                'SELECT id,label FROM vainglory_heroes ORDER BY id'
            )
        }

        assert labels == {1: 'Phinn', 2: '自定义名称', 3: ''}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_consolidates_named_heroes_and_prunes_unknown_orphans(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO vainglory_heroes('
            'id,fingerprint,thumbnail_png,label,created_at,updated_at) '
            "VALUES(1,'0000000000000001',X'01','Caine',1,1),"
            "(2,'0000000000000002',X'02','caine',1,2),"
            "(3,'0000000000000003',X'03','',1,3)"
        )
        repository = VaingloryRepository(database, clock=lambda: 100)

        assert await repository.consolidate_hero_catalog() == 2
        heroes = await repository.list_heroes()

        assert [(hero.id, hero.label, hero.fingerprint) for hero in heroes] == [
            (1, 'Caine', '0000000000000002')
        ]
        assert await repository.hero_thumbnail(1) == b'\x02'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_searches_all_visual_variants_with_same_label(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        repository = VaingloryRepository(database, clock=lambda: 100)
        await repository.sync_hero_references(
            (HeroReference('Caine', 'a' * 64, b'\xff\xd8reference'),)
        )
        await repository.request_scan(1)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))
        used_hero_id = (await repository.list_matches()).items[0].players[0].hero_id
        assert used_hero_id is not None
        await repository.label_hero(used_hero_id, '同一英雄')
        await database.execute(
            'INSERT INTO vainglory_heroes('
            'fingerprint,thumbnail_png,label,created_at,updated_at) '
            "VALUES('ffffffffffffffff',X'01','同一英雄',100,100)"
        )
        alternate_id = int(
            await database.scalar(
                "SELECT id FROM vainglory_heroes WHERE fingerprint='ffffffffffffffff'"
            )
        )

        page = await repository.list_matches(hero_ids=(alternate_id,))

        assert page.total == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_completes_parts_without_replacing_previous_matches(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        first = tmp_path / 'p1.mp4'
        second = tmp_path / 'p2.mp4'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        await seed_session(database, first, state='open')
        repository = VaingloryRepository(database, clock=lambda: 100)

        first_claim = await repository.claim_next()
        assert first_claim is not None
        await repository.complete_part(1, (analyzed_match(),))
        await seed_part(database, second, session_id=1, part_id=2, part_index=2)

        second_claim = await repository.claim_next()
        assert second_claim is not None
        assert second_claim.part.id == 2
        second_match = replace(
            analyzed_match(), part_id=2, part_index=2, result_at_ms=1_000_000
        )
        await repository.complete_part(2, (second_match,))

        page = await repository.list_matches(session_id=1)
        assert page.total == 2
        assert {match.part_id for match in page.items} == {1, 2}
        job = await repository.get_job(1)
        assert job is not None
        assert job.state == 'ready'
        assert job.match_count == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_session_job_stays_in_progress_until_every_archive_page_finishes(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        first = tmp_path / 'p1.mp4'
        second = tmp_path / 'p2.mp4'
        third = tmp_path / 'p3.mp4'
        for path in (first, second, third):
            path.write_bytes(b'video')
        await seed_session(database, first)
        repository = VaingloryRepository(database, clock=lambda: 100)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))
        await seed_part(database, second, session_id=1, part_id=2, part_index=2)
        await seed_part(database, third, session_id=1, part_id=3, part_index=3)
        await database.execute(
            "INSERT INTO bili_accounts("
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            "state,created_at,updated_at) VALUES(1,42,'账号',X'00',1,'key',"
            "'active',1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,published_at,session_id,state,progress,'
            'page_count,completed_page_count,created_at,updated_at) '
            "VALUES(1,1,303,'BV1abcdefgh','历史稿件',1,1,'analyzing',?,3,1,1,1)",
            (1 / 3,),
        )
        for page, state in ((1, 'ready'), (2, 'queued'), (3, 'queued')):
            await database.execute(
                'INSERT INTO vainglory_archive_parts('
                'import_id,page,cid,title,duration_seconds,recording_part_id,'
                'state,progress,created_at,updated_at) '
                'VALUES(1,?,?,?,600,?,?,?,1,1)',
                (
                    page,
                    400 + page,
                    'P{}'.format(page),
                    page,
                    state,
                    1 if state == 'ready' else 0,
                ),
            )

        await database.write(
            lambda connection: repository._refresh_session_job(connection, 1, 100)
        )

        row = await database.fetchone(
            'SELECT state,progress,completed_at FROM vainglory_scan_jobs '
            'WHERE session_id=1'
        )
        assert row is not None
        assert str(row['state']) == 'analyzing'
        assert float(row['progress']) == pytest.approx(1 / 3)
        assert row['completed_at'] is None
        assert (await repository.list_match_sessions()).total == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_uses_upload_title_until_match_title_is_overridden(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        await database.execute(
            "INSERT INTO bili_accounts("
            "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
            "state,created_at,updated_at) "
            "VALUES(1,42,'账号',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'created_at,updated_at) '
            "VALUES(1,1,1,?,'approved','confirmed',1,1)",
            ('{"title":"最终投稿标题"}',),
        )
        repository = VaingloryRepository(database, clock=lambda: 100)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))

        automatic = (await repository.list_matches()).items[0]
        assert automatic.title == '最终投稿标题'
        assert automatic.source_title == '样本录播'
        assert automatic.upload_title == '最终投稿标题'

        edited = await repository.update_match_title(automatic.id, '  单独的对局标题  ')
        assert edited.title == '单独的对局标题'
        reset = await repository.update_match_title(automatic.id, '')
        assert reset.title == '最终投稿标题'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_exposes_historical_archive_source(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        await database.execute(
            "INSERT INTO bili_accounts("
            "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
            "state,created_at,updated_at) "
            "VALUES(1,42,'旧账号',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_video_sources('
            'part_id,account_id,bvid,cid,page,origin,state,retention_kind,'
            'progress,downloaded_bytes,original_artifact_state,created_at,'
            'updated_at) '
            "VALUES(1,1,'BV1abcdefgh',123,2,'archive','missing','analysis',"
            "0,0,'missing',1,1)"
        )
        repository = VaingloryRepository(database, clock=lambda: 100)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))

        match = (await repository.list_matches()).items[0]

        assert match.account_id == 1
        assert match.bvid == 'BV1abcdefgh'
        assert match.archive_page == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_filters_game_mode(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        repository = VaingloryRepository(database, clock=lambda: 100)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))

        assert (await repository.list_matches(game_mode='3v3')).total == 1
        assert (await repository.list_matches(game_mode='5v5')).total == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_lists_one_summary_per_recording_session(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        first = tmp_path / 'first.mp4'
        second = tmp_path / 'second.mp4'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        await seed_session(database, first, session_id=1)
        await seed_session(database, second, session_id=2)
        repository = VaingloryRepository(database, clock=lambda: 100)

        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))
        assert await repository.claim_next() is not None
        other_players = tuple(
            replace(player, name='另一位玩家', normalized_name='另一位玩家')
            for player in analyzed_match().ocr.players
        )
        other_match = replace(
            analyzed_match(),
            part_id=2,
            ocr=replace(analyzed_match().ocr, players=other_players),
        )
        await repository.complete_part(2, (other_match,))

        page = await repository.list_match_sessions()
        filtered = await repository.list_match_sessions(player_name='玩家12')

        assert page.total == 2
        assert [item.session_id for item in page.items] == [2, 1]
        assert all(item.match_count == 1 for item in page.items)
        assert all(item.teal_win_count == 1 for item in page.items)
        assert page.items[0].game_modes == ('3v3',)
        assert page.items[0].win_count == 1
        assert page.items[0].loss_count == 0
        assert page.items[0].anchor_name == '样本主播'
        assert filtered.total == 1
        assert filtered.items[0].session_id == 1

        renamed = await repository.update_session_title(1, '  整场标题  ')
        assert renamed.title == '整场标题'
        assert renamed.source_title == '样本录播'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_invalidates_old_results_and_removes_unknown_catalog(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'sample.mp4'
        video.write_bytes(b'video')
        await seed_session(database, video)
        repository = VaingloryRepository(database, clock=lambda: 100)
        await repository.sync_hero_references(
            (HeroReference('Caine', 'a' * 64, b'\xff\xd8reference'),)
        )
        await repository.request_scan(1)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))
        result = (await repository.list_matches()).items[0]
        frame = await repository.result_frame_path(result.id)
        assert frame is not None and frame.is_file()
        await database.execute(
            'UPDATE vainglory_part_jobs SET algorithm_version=? WHERE part_id=1',
            (repository.ALGORITHM_VERSION - 1,),
        )
        await database.execute(
            'INSERT INTO vainglory_heroes('
            'fingerprint,thumbnail_png,label,created_at,updated_at) '
            "VALUES('ffffffffffffffff',X'01','',1,1)"
        )

        invalidated = await repository.invalidate_outdated_results()
        await repository.consolidate_hero_catalog()

        assert invalidated == 1
        assert (await repository.list_matches()).total == 0
        assert not frame.exists()
        assert [hero.label for hero in await repository.list_heroes()] == ['Caine']
        row = await database.fetchone(
            'SELECT state,algorithm_version,match_count '
            'FROM vainglory_part_jobs WHERE part_id=1'
        )
        assert row is not None
        assert (
            str(row['state']),
            int(row['algorithm_version']),
            int(row['match_count']),
        ) == ('pending', repository.ALGORITHM_VERSION, 0)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_aggregates_results_by_anchor_identity(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        first = tmp_path / 'first.mp4'
        second = tmp_path / 'second.mp4'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        await seed_session(database, first, session_id=1)
        await seed_session(database, second, session_id=2)
        await database.execute(
            "UPDATE recording_sessions SET anchor_uid=42,anchor_name='主播甲' "
            'WHERE id IN (1,2)'
        )
        repository = VaingloryRepository(database, clock=lambda: 100)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))
        assert await repository.claim_next() is not None
        lost = replace(
            analyzed_match(),
            part_id=2,
            layout=replace(
                analyzed_match().layout,
                winner_color='orange',
                winner_side='left',
                left_color='orange',
                right_color='teal',
            ),
        )
        await repository.complete_part(2, (lost,))

        stats = await repository.list_anchor_stats()

        assert len(stats) == 1
        assert stats[0].anchor_uid == 42
        assert stats[0].anchor_name == '主播甲'
        assert stats[0].session_count == 2
        assert stats[0].match_count == 2
        assert stats[0].win_count == 1
        assert stats[0].loss_count == 1
        assert stats[0].win_rate == 0.5

        assert await repository.bulk_update_sessions([2], stats_included=False) == 1
        filtered = await repository.list_match_sessions(stats_included=False)
        remaining_stats = await repository.list_anchor_stats()

        assert filtered.total == 1
        assert filtered.items[0].session_id == 2
        assert filtered.items[0].stats_included is False
        assert len(remaining_stats) == 1
        assert remaining_stats[0].session_count == 1
        assert remaining_stats[0].match_count == 1
        assert remaining_stats[0].win_count == 1
        assert remaining_stats[0].loss_count == 0

        renamed = await repository.update_session_anchor(2, '迟昭义')
        assert renamed.anchor_name == '迟昭义'
        identity = await database.fetchone(
            'SELECT room_id,anchor_uid FROM recording_sessions WHERE id=2'
        )
        assert identity is not None
        assert int(identity['room_id']) == 0
        assert identity['anchor_uid'] is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_match_level_exclusion_keeps_match_but_filters_all_statistics(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        first = tmp_path / 'first.mp4'
        second = tmp_path / 'second.mp4'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        await seed_session(database, first, session_id=1)
        await seed_session(database, second, session_id=2)
        await database.execute(
            "UPDATE recording_sessions SET anchor_uid=42,anchor_name='主播甲' "
            'WHERE id IN (1,2)'
        )
        repository = VaingloryRepository(database, clock=lambda: 100)
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (analyzed_match(),))
        assert await repository.claim_next() is not None
        excluded = replace(
            analyzed_match(),
            part_id=2,
            match_kind='bot',
            stats_eligible=False,
            stats_exclusion_reason='bot',
            layout=replace(
                analyzed_match().layout,
                winner_color='orange',
                winner_side='left',
                left_color='orange',
                right_color='teal',
            ),
        )
        await repository.complete_part(2, (excluded,))

        page = await repository.list_matches()
        stats = await repository.list_anchor_stats()
        summary = await repository.index_summary()

        assert page.total == 2
        excluded_record = next(item for item in page.items if item.part_id == 2)
        assert excluded_record.match_kind == 'bot'
        assert excluded_record.stats_eligible is False
        assert excluded_record.stats_exclusion_reason == 'bot'
        assert len(stats) == 1
        assert stats[0].match_count == 1
        assert stats[0].win_count == 1
        assert stats[0].loss_count == 0
        assert summary.match_count == 2
        assert summary.win_count == 1
        assert summary.loss_count == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repository_manages_players_and_aggregates_player_rankings(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        first = tmp_path / 'first.mp4'
        second = tmp_path / 'second.mp4'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        await seed_session(database, first, session_id=1)
        await seed_session(database, second, session_id=2)
        await database.execute(
            "UPDATE recording_sessions SET anchor_uid=42,anchor_name='直播名称' "
            'WHERE id IN (1,2)'
        )
        repository = VaingloryRepository(database, clock=lambda: 100)
        await repository.sync_hero_references(
            (HeroReference('Caine', 'a' * 64, b'\xff\xd8reference'),)
        )
        recorded = replace(
            analyzed_match(),
            recorded_player=RecordedPlayer(side='right', slot=2, confidence=0.95),
        )
        assert await repository.claim_next() is not None
        await repository.complete_part(1, (recorded,))
        assert await repository.claim_next() is not None
        lost = replace(
            recorded,
            part_id=2,
            layout=replace(
                recorded.layout,
                winner_color='orange',
                winner_side='left',
                left_color='orange',
                right_color='teal',
            ),
        )
        await repository.complete_part(2, (lost,))

        automatic = await repository.list_players()

        assert len(automatic) == 1
        assert automatic[0].name == '直播名称'
        assert automatic[0].origin == 'automatic'
        assert [room.room_id for room in automatic[0].rooms] == [100]

        renamed = await repository.rename_player(automatic[0].id, '  游戏名称  ')
        assert renamed.name == '游戏名称'
        injected = await repository.create_player("'; DROP TABLE players; --")
        assert injected.name == "'; DROP TABLE players; --"
        assert await database.scalar('SELECT COUNT(*) FROM vainglory_players') == 2

        manual = await repository.create_player('手动玩家')
        rebound = await repository.bind_player_room(manual.id, 100)
        rebound = await repository.bind_player_room(manual.id, 200)
        assert [room.room_id for room in rebound.rooms] == [100, 200]
        rebound = await repository.unbind_player_room(manual.id, 200)
        assert [room.room_id for room in rebound.rooms] == [100]

        player_stats = await repository.list_player_stats()
        manual_stats = next(
            item for item in player_stats if item.player_id == manual.id
        )
        assert manual_stats.player_name == '手动玩家'
        assert manual_stats.session_count == 2
        assert manual_stats.match_count == 2
        assert manual_stats.win_count == 1
        assert manual_stats.loss_count == 1
        assert manual_stats.win_rate == 0.5
        assert [(mode.game_mode, mode.match_count) for mode in manual_stats.modes] == [
            ('3v3', 2)
        ]
        assert len(manual_stats.heroes) == 1
        assert manual_stats.heroes[0].hero_label == 'Caine'
        assert manual_stats.heroes[0].match_count == 2

        hero_stats = await repository.list_hero_stats(game_mode='3v3')
        assert len(hero_stats) == 1
        assert hero_stats[0].hero_label == 'Caine'
        assert hero_stats[0].player_count == 1
        assert hero_stats[0].match_count == 2
        assert hero_stats[0].win_count == 1
        assert hero_stats[0].loss_count == 1
        assert hero_stats[0].win_rate == 0.5

        with pytest.raises(VaingloryNotFound):
            await repository.rename_player(999, '不存在')
    finally:
        await database.close()
