import json
from dataclasses import replace
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

import pytest

from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.errors import BiliApiError, RemoteOutcomeUnknown
from blrec.vainglory.analyzer import AnalyzedHero, AnalyzedMatch
from blrec.vainglory.hero_recognition import HeroReference
from blrec.vainglory.ocr import OcrPlayer, PlayerStats, ResultHeader, ResultOcr
from blrec.vainglory.publication import (
    DESCRIPTION_BEGIN,
    DESCRIPTION_END,
    VaingloryPublicationService,
    _automatic_chapter_cards,
    _chapter_content,
    _match_anchor,
    build_publication_plan,
    description_contains_block,
    merge_archive_description,
)
from blrec.vainglory.repository import (
    MatchPlayerRecord,
    MatchRecord,
    VaingloryRepository,
)
from blrec.vainglory.vision import ResultLayout


def player(side: str, slot: int, name: str, hero: str) -> MatchPlayerRecord:
    return MatchPlayerRecord(
        side=side,
        slot=slot,
        name=name,
        normalized_name=name.casefold(),
        hero_id=slot,
        hero_label=hero,
        hero_source='automatic',
        kills=1,
        deaths=2,
        assists=3,
        economy=1000,
        confidence=0.9,
        last_hits=12,
    )


def match(
    match_id: int,
    *,
    winner_color: str = 'teal',
    page: int = 1,
    start_ms: int = 120_000,
    result_ms: int = 720_000,
    duration_seconds: Optional[int] = 600,
    frame: bool = True,
) -> MatchRecord:
    players: Tuple[MatchPlayerRecord, ...] = (
        player('left', 1, '蓝方玩家', 'Caine'),
        player('right', 1, '红方玩家', 'Krul'),
    )
    return MatchRecord(
        id=match_id,
        session_id=10,
        session_title='直播回放',
        session_started_at=100,
        part_id=20 + page,
        part_index=page,
        title='第 {} 局'.format(match_id),
        source_title='直播回放',
        upload_title='直播回放',
        game_mode='3v3',
        team_size=1,
        started_at_ms=start_ms,
        result_at_ms=result_ms,
        duration_seconds=duration_seconds,
        result_text='Victory',
        end_reason='normal',
        left_color='teal',
        right_color='orange',
        winner_side='left' if winner_color == 'teal' else 'right',
        winner_color=winner_color,
        left_kills=12,
        right_kills=8,
        left_economy=30_000,
        right_economy=20_000,
        confidence=0.9,
        account_id=1,
        bvid='BV1abcdefgh',
        archive_page=page,
        has_result_frame=frame,
        recorded_player_confidence=None,
        recorded_player_source='automatic',
        players=players,
    )


def test_publication_uses_clean_description_and_comment_native_timestamps() -> None:
    plan = build_publication_plan((match(2, winner_color='orange', page=2), match(1)))

    assert '共 2 局｜1 胜 1 负' in plan.description_block
    assert '蓝方' not in plan.description_block
    assert '红方' not in plan.description_block
    assert '蓝方玩家' not in plan.description_block
    assert '红方玩家' not in plan.description_block
    assert DESCRIPTION_BEGIN not in plan.description_block
    assert DESCRIPTION_END not in plan.description_block
    assert '①｜胜　｜3V3｜凯恩 vs 骷髅' in plan.description_block
    assert '②｜负　｜3V3｜凯恩 vs 骷髅' in plan.description_block
    assert '#02:00' not in plan.description_block
    assert '1/2/3' not in plan.description_block
    assert '经济' not in plan.description_block
    assert '补刀' not in plan.description_block
    assert '第1局：https://www.bilibili.com/video/BV1abcdefgh?p=1&t=120' in (
        plan.description_block
    )
    assert plan.comments[0].content.startswith('共 2 局｜1 胜 1 负\n')
    assert '①｜胜　｜3V3｜凯恩 vs 骷髅｜1#02:00' in plan.comments[0].content
    assert '②｜负　｜3V3｜凯恩 vs 骷髅｜2#02:00' in plan.comments[0].content
    assert plan.comments[0].match_ids == (1, 2)


def test_publication_hash_ignores_database_ids_but_tracks_result_picture() -> None:
    first = match(1)
    reinserted = replace(first, id=999)

    original = build_publication_plan((first,), frame_hashes={first.id: 'a' * 64})
    same_output = build_publication_plan(
        (reinserted,), frame_hashes={reinserted.id: 'a' * 64}
    )
    changed_picture = build_publication_plan(
        (reinserted,), frame_hashes={reinserted.id: 'b' * 64}
    )

    assert same_output.payload_hash == original.payload_hash
    assert changed_picture.payload_hash != original.payload_hash


def test_zero_match_publication_plan_is_a_real_clear_revision() -> None:
    plan = build_publication_plan((), bvid='BV1abcdefgh')

    assert plan.match_count == 0
    assert plan.comments == ()
    assert plan.description_block == '共 0 局｜0 胜 0 负'
    assert json.loads(plan.analysis_snapshot_json)['matches'] == []


def test_publication_omits_unreliable_direct_link() -> None:
    plan = build_publication_plan(
        (match(1, start_ms=0), match(2, duration_seconds=300))
    )

    assert '#02:00' not in plan.description_block


def test_unknown_result_is_not_counted_as_a_loss() -> None:
    plan = build_publication_plan((match(1, winner_color='unknown'),))

    assert '共 1 局｜0 胜 0 负｜1 局结果未确认' in plan.description_block
    assert '①｜待定｜3V3｜凯恩 vs 骷髅' in plan.description_block
    assert '①｜待定｜3V3｜凯恩 vs 骷髅｜1#02:00' in plan.comments[0].content


def test_chapter_uses_full_chinese_hero_name_with_game_number() -> None:
    current = match(1)
    recorded = replace(current.players[0], hero_label='Grace', is_recorded_player=True)
    current = replace(current, players=(recorded, *current.players[1:]))

    content = _chapter_content(1, current)

    assert content == '第一局｜胜｜格瑞丝｜3V3'
    assert len(content) <= 16
    assert _automatic_chapter_cards(({'content': content},)) is True
    assert _automatic_chapter_cards(({'content': '1胜|锤妈'},)) is True


def test_comment_keeps_all_results_in_first_comment_and_splits_only_pictures() -> None:
    plan = build_publication_plan(tuple(match(index) for index in range(1, 12)))

    assert tuple(
        match_id for comment in plan.comments for match_id in comment.match_ids
    ) == tuple(range(1, 12))
    assert tuple(item.match_ids for item in plan.comments) == (
        tuple(range(1, 10)),
        (10, 11),
    )
    assert all(
        chr(0x2460 + match_id - 1) in plan.comments[0].content
        for match_id in range(1, 12)
    )
    assert plan.comments[1].content == '结算截图（续 1）'
    assert '第10局' not in plan.comments[1].content
    assert all(len(item.content) <= 1000 for item in plan.comments)


def test_native_timestamp_supports_hours_and_multipart_seek() -> None:
    plan = build_publication_plan(
        (match(1, page=3, start_ms=3_723_000, result_ms=4_323_000),)
    )

    assert '3#01:02:03' not in plan.description_block
    assert '①｜胜　｜3V3｜凯恩 vs 骷髅｜3#01:02:03' in plan.comments[0].content


def test_match_anchor_infers_same_page_start_from_result_ocr_duration() -> None:
    current = match(1, start_ms=0, result_ms=720_000, duration_seconds=600)

    assert _match_anchor(current) == (1, 120)


def test_match_anchor_walks_previous_pages_from_nearest_to_oldest() -> None:
    current = replace(
        match(1, page=3, start_ms=0, result_ms=100_000, duration_seconds=500),
        previous_archive_segments=((1, 1_200), (2, 300)),
    )

    assert _match_anchor(current) == (1, 1_100)


def test_match_anchor_marks_recording_start_when_live_began_mid_match() -> None:
    current = match(1, start_ms=0, result_ms=100_000, duration_seconds=500)

    assert _match_anchor(current) == (1, 0)


def test_very_long_archive_keeps_every_result_in_first_comment() -> None:
    plan = build_publication_plan(tuple(match(index) for index in range(1, 101)))

    assert '逐局（按顺序）：' in plan.comments[0].content
    assert plan.comments[0].content.endswith('胜' * 100)
    assert len(plan.comments[0].content) <= 1000
    assert all(len(comment.match_ids) <= 9 for comment in plan.comments)


def test_description_append_is_markerless_and_idempotent() -> None:
    first_block = '第一版'
    original = '  原简介\n第二行  '

    appended = merge_archive_description(original, first_block)
    assert appended == original + '\n\n' + first_block
    assert appended is not None
    assert description_contains_block(appended, first_block)

    assert merge_archive_description(appended, first_block) == appended


def test_description_replaces_legacy_marked_block_without_visible_markers() -> None:
    original = '  原简介\n第二行  '
    legacy = '\n'.join(
        (
            DESCRIPTION_BEGIN,
            '共 1 局｜1 胜 0 负',
            '第1局 1#02:00｜胜｜凯恩 vs 骷髅',
            DESCRIPTION_END,
        )
    )
    current = original + '\n\n' + legacy
    new_block = '共 1 局｜1 胜 0 负\n第1局｜胜｜凯恩 vs 骷髅'

    replaced = merge_archive_description(current, new_block)

    assert replaced == original + '\n\n' + new_block
    assert DESCRIPTION_BEGIN not in replaced
    assert DESCRIPTION_END not in replaced


def test_description_never_truncates_existing_user_text() -> None:
    original = '用户' * 950
    block = '\n'.join(
        ('共 20 局｜10 胜 10 负',)
        + tuple('第 {} 局内容'.format(index) for index in range(20))
    )

    merged = merge_archive_description(original, block)

    assert merged is not None
    assert merged.startswith(original + '\n\n')
    assert '…其余对局请见置顶评论' in merged
    assert len(merged) <= 2000

    assert merge_archive_description('x' * 2000, block) is None


class FakePublicationProtocol:
    def __init__(self) -> None:
        self.description = '  原简介\n第二行  '
        self.public_archive_calls: List[str] = []
        self.public_archive_result: Any = {
            'code': 0,
            'data': {
                'aid': 303,
                'bvid': 'BV1abcdefgh',
                'pubdate': 900,
                'pages': [{'cid': 401, 'page': 1, 'duration': 1200}],
            },
        }
        self.edit_calls: List[Mapping[str, Any]] = []
        self.picture_calls: List[str] = []
        self.add_reply_calls: List[Mapping[str, Any]] = []
        self.top_reply_calls: List[Mapping[str, Any]] = []
        self.delete_reply_calls: List[Mapping[str, Any]] = []
        self.list_replies_calls: List[Mapping[str, Any]] = []
        self.chapter_calls: List[Mapping[str, Any]] = []
        self.chapter_batches: List[Tuple[Mapping[str, Any], ...]] = []
        self.chapter_cards: Tuple[Mapping[str, Any], ...] = ()
        self.add_reply_result: Any = {'code': 0, 'data': {'rpid': 501}}
        self.list_replies_result: Mapping[str, Any] = {
            'code': 0,
            'data': {'replies': []},
        }

    async def public_archive_view(
        self, _bundle: object, *, bvid: str
    ) -> Mapping[str, Any]:
        self.public_archive_calls.append(bvid)
        if isinstance(self.public_archive_result, Exception):
            raise self.public_archive_result
        return self.public_archive_result

    async def archive_view(
        self, _bundle: object, _params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {
            'code': 0,
            'data': {
                'archive': {
                    'aid': 303,
                    'bvid': 'BV1abcdefgh',
                    'title': '原标题',
                    'desc': self.description,
                    'tag': '虚荣,直播回放',
                    'tid': 171,
                    'copyright': 1,
                    'cover': 'https://i0.hdslb.com/cover.jpg',
                },
                'subtitle': {'open': 1, 'lan': 'zh-CN'},
                'videos': [
                    {
                        'filename': 'remote-p1',
                        'title': 'P1',
                        'desc': '',
                        'cid': 401,
                        'duration': 1200,
                    }
                ],
            },
        }

    async def archive_cards(
        self, _bundle: object, *, aid: int, cid: int
    ) -> Mapping[str, Any]:
        assert (aid, cid) == (303, 401)
        return {
            'code': 0,
            'data': {'catalog': [{'type': 2, 'cards': list(self.chapter_cards)}]},
        }

    async def submit_archive_chapters(
        self,
        _bundle: object,
        *,
        aid: int,
        cid: int,
        cards: Tuple[Mapping[str, Any], ...],
        permanent: bool,
    ) -> Mapping[str, Any]:
        assert (aid, cid, permanent) == (303, 401, True)
        self.chapter_batches.append(tuple(cards))
        self.chapter_cards = tuple(cards)
        self.chapter_calls.extend(cards)
        return {'code': 0}

    async def edit_archive(
        self, _bundle: object, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.edit_calls.append(dict(payload))
        self.description = str(payload['desc'])
        return {'code': 0, 'data': {'aid': 303, 'bvid': 'BV1abcdefgh'}}

    async def upload_comment_picture(
        self, _bundle: object, *, filename: str, mime_type: str, content: bytes
    ) -> Mapping[str, Any]:
        assert mime_type == 'image/png'
        assert content.startswith(b'\x89PNG')
        self.picture_calls.append(filename)
        return {
            'img_src': 'https://i0.hdslb.com/{}'.format(filename),
            'img_width': 1920,
            'img_height': 1080,
            'img_size': 1.0,
        }

    async def add_reply(
        self, _bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.add_reply_calls.append(dict(params))
        if isinstance(self.add_reply_result, BaseException):
            result = self.add_reply_result
            self.add_reply_result = {'code': 0, 'data': {'rpid': 501}}
            raise result
        return self.add_reply_result

    async def top_reply(
        self, _bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.top_reply_calls.append(dict(params))
        return {'code': 0}

    async def delete_reply(
        self, _bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.delete_reply_calls.append(dict(params))
        return {'code': 0}

    async def list_replies(
        self, _bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.list_replies_calls.append(dict(params))
        return self.list_replies_result

    async def reply_detail(
        self, _bundle: object, _params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {
            'code': 0,
            'data': {
                'root': {
                    'rpid': 501,
                    'content': {'pictures': [{'img_src': 'result.png'}]},
                }
            },
        }


async def seed_publication_match(
    database: BiliUploadDatabase, tmp_path: Path
) -> VaingloryRepository:
    video = tmp_path / 'sample.mp4'
    video.write_bytes(b'video')
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,state,'
        'created_at,updated_at) '
        "VALUES(1,42,'投稿账号',X'00',1,'key','active',1,1)"
    )
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at,title,anchor_name) '
        "VALUES(1,100,'100:1','closed',1,'直播回放','主播')"
    )
    await database.execute(
        'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
        "VALUES('run:1',1,'finished',1,2)"
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,record_start_time,'
        'artifact_state,created_at,updated_at) '
        "VALUES(1,1,'run:1',1,?,1,'ready',1,1)",
        (str(video),),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'aid,bvid,created_at,updated_at) '
        "VALUES(1,1,1,'{}','approved','confirmed',303,'BV1abcdefgh',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'job_id,part_index,source_path,artifact_state,upload_state,'
        'remote_filename,cid) '
        "VALUES(1,1,?,'ready','confirmed','remote-p1',401)",
        (str(video),),
    )
    repository = VaingloryRepository(
        database, result_frame_root=tmp_path / 'frames', clock=lambda: 1000
    )
    await repository.sync_hero_references(
        (
            HeroReference('Caine', 'a' * 64, b'caine'),
            HeroReference('Krul', 'b' * 64, b'krul'),
        )
    )
    await repository.request_scan(1)
    claim = await repository.claim_next()
    assert claim is not None
    players = (
        OcrPlayer(
            side='left',
            slot=1,
            name='蓝方玩家',
            normalized_name='蓝方玩家',
            stats=PlayerStats(1, 2, 3, 1000, 12),
            confidence=0.9,
        ),
        OcrPlayer(
            side='right',
            slot=1,
            name='红方玩家',
            normalized_name='红方玩家',
            stats=PlayerStats(2, 1, 3, 900, 10),
            confidence=0.9,
        ),
    )
    analyzed = AnalyzedMatch(
        part_id=1,
        part_index=1,
        result_at_ms=720_000,
        layout=ResultLayout('teal', 'orange', 'teal', 'left', 1.0),
        ocr=ResultOcr(
            ResultHeader('Victory', 'normal', 600, 8, 4, 20_000, 15_000), players
        ),
        heroes=(
            AnalyzedHero('left', 1, 'a' * 64, b'caine', 'Caine'),
            AnalyzedHero('right', 1, 'b' * 64, b'krul', 'Krul'),
        ),
        confidence=0.95,
        result_frame_png=b'\x89PNG-result',
    )
    await repository.complete_part(1, (analyzed,))
    return repository


async def async_bundle(_account_id: int) -> object:
    return object()


@pytest.mark.asyncio
async def test_service_preserves_description_posts_picture_and_pins_once(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        protocol = FakePublicationProtocol()
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        for _ in range(6):
            assert await service.run_once() is True

        assert protocol.description.startswith('  原简介\n第二行  \n\n')
        assert len(protocol.edit_calls) == 1
        assert protocol.edit_calls[0]['subtitle'] == {'open': 1, 'lan': 'zh-CN'}
        assert len(protocol.picture_calls) == 1
        assert len(protocol.add_reply_calls) == 1
        assert '图片' not in protocol.add_reply_calls[0]['message']
        assert 'pictures' in protocol.add_reply_calls[0]
        assert protocol.top_reply_calls == [
            {'type': 1, 'oid': 303, 'rpid': 501, 'action': 1}
        ]
        assert (
            await database.scalar('SELECT state FROM vainglory_publications')
            == 'confirmed'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_chapters_use_result_ocr_duration_when_stored_start_is_missing(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute('UPDATE vainglory_matches SET started_at_ms=0')
        protocol = FakePublicationProtocol()
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        assert await service.run_once() is True
        assert await service.run_once() is True

        assert (
            await database.scalar('SELECT chapter_state FROM vainglory_publications')
            == 'confirmed'
        )
        assert await database.scalar('SELECT error FROM vainglory_publications') is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_chapters_mark_missing_ocr_duration_for_reanalysis(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute(
            'UPDATE vainglory_matches SET started_at_ms=0,duration_seconds=NULL'
        )
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        assert await service.run_once() is True

        status = (await service.publication_statuses((1,)))[1]
        assert status.code == 'analysis_data_invalid'
        assert status.recommended_action == 'reanalyze'
        assert (
            await database.scalar('SELECT chapter_state FROM vainglory_publications')
            == 'prepared'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_upload_publication_waits_until_bilibili_review_is_approved(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute("UPDATE upload_jobs SET state='waiting_review'")
        protocol = FakePublicationProtocol()
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        assert await service.run_once() is False
        assert protocol.chapter_batches == []
        status = (await service.publication_statuses((1,)))[1]
        assert status.code == 'waiting_review'
        assert status.recommended_action == 'wait'

        await database.execute("UPDATE upload_jobs SET state='approved'")
        assert await service.run_once() is True
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_upload_publication_waits_until_archive_is_publicly_visible(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        protocol = FakePublicationProtocol()
        protocol.public_archive_result = BiliApiError(-404)
        now = [1000]
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: now[0],
        )

        assert await service.run_once() is True
        assert await service.run_once() is True

        publication = await database.fetchone(
            'SELECT state,next_attempt_at,error,public_visible_at '
            'FROM vainglory_publications'
        )
        assert dict(publication) == {
            'state': 'paused',
            'next_attempt_at': 1300,
            'error': '稿件尚未公开，公开可访问后自动处理简介、评论和视频分段',
            'public_visible_at': None,
        }
        status = (await service.publication_statuses((1,)))[1]
        assert status.code == 'waiting_publication'
        assert status.recommended_action == 'wait'
        assert protocol.public_archive_calls == ['BV1abcdefgh']
        assert protocol.list_replies_calls == []
        assert protocol.chapter_batches == []
        assert protocol.edit_calls == []
        assert protocol.add_reply_calls == []

        assert await service.run_once() is False
        protocol.public_archive_result = {
            'code': 0,
            'data': {
                'aid': 303,
                'bvid': 'BV1abcdefgh',
                'pubdate': 1200,
                'pages': [{'cid': 401, 'page': 1, 'duration': 1200}],
            },
        }
        now[0] = 1300
        assert await service.run_once() is True
        assert protocol.public_archive_calls == ['BV1abcdefgh', 'BV1abcdefgh']
        assert protocol.chapter_batches
        assert (
            await database.scalar(
                'SELECT public_visible_at FROM vainglory_publications'
            )
            == 1300
        )

        assert await service.run_once() is True
        assert len(protocol.public_archive_calls) == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_upload_publication_waits_for_scheduled_publication_time(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        protocol = FakePublicationProtocol()
        protocol.public_archive_result = {
            'code': 0,
            'data': {
                'aid': 303,
                'bvid': 'BV1abcdefgh',
                'pubdate': 1200,
                'pages': [{'cid': 401, 'page': 1, 'duration': 1200}],
            },
        }
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        assert await service.run_once() is True

        assert protocol.public_archive_calls == ['BV1abcdefgh']
        assert protocol.list_replies_calls == []
        assert protocol.chapter_batches == []
        assert (
            await database.scalar('SELECT error FROM vainglory_publications')
            == '稿件尚未公开，公开可访问后自动处理简介、评论和视频分段'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_publication_status_distinguishes_retry_from_bad_analysis_data(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )
        assert await service.run_once() is True
        await database.execute(
            "UPDATE vainglory_publications SET plan_state='waiting_analysis'"
        )
        await database.execute(
            "UPDATE vainglory_scan_jobs SET state='pending',progress=0,"
            'started_at=NULL,completed_at=NULL'
        )

        waiting = (await service.publication_statuses((1,)))[1]
        assert waiting.code == 'waiting_analysis'
        assert waiting.recommended_action == 'wait'

        await database.execute(
            "UPDATE upload_jobs SET state='rejected',review_reason='"
            '稿件内容未通过审核'
            "'"
        )
        rejected = (await service.publication_statuses((1,)))[1]
        assert rejected.code == 'review_rejected'
        assert rejected.detail == '稿件内容未通过审核'
        await database.execute("UPDATE upload_jobs SET state='approved'")

        await database.execute(
            "UPDATE vainglory_publications SET plan_state='ready',state='paused',"
            'next_attempt_at=1600,'
            "error='B 站章节请求未发出，将自动重试'"
        )

        retrying = (await service.publication_statuses((1,)))[1]
        assert retrying.code == 'retry_scheduled'
        assert retrying.recommended_action == 'wait'
        assert retrying.next_attempt_at == 1600

        await database.execute(
            "UPDATE vainglory_publications SET error='"
            '部分对局缺少有效时间点，视频分段不会跳过并将自动重试'
            "'"
        )
        legacy = (await service.publication_statuses((1,)))[1]
        assert legacy.code == 'legacy_chapter_timing'
        assert legacy.recommended_action == 'retry_chapter'

        assert await service._requeue_legacy_chapter_timing() == 1
        assert (
            await database.scalar('SELECT state FROM vainglory_publications')
            == 'prepared'
        )

        await database.execute(
            "UPDATE vainglory_publications SET state='failed',"
            "error='1 局识别结果缺少结算画面时间或 OCR 对局时长，请重新分析这场直播'"
        )

        invalid = (await service.publication_statuses((1,)))[1]
        assert invalid.code == 'analysis_data_invalid'
        assert invalid.recommended_action == 'reanalyze'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_zero_match_reanalysis_clears_old_generated_content_and_keeps_history(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        protocol = FakePublicationProtocol()
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        old_block = str(
            await database.scalar(
                'SELECT description_block FROM vainglory_publications'
            )
        )
        protocol.description = '用户简介\n\n' + old_block
        protocol.chapter_cards = (
            {'from': 0, 'to': 120, 'content': '直播开始'},
            {'from': 120, 'to': 1200, 'content': '第一局｜胜｜凯恩｜3V3'},
        )
        protocol.list_replies_result = {
            'code': 0,
            'data': {
                'cursor': {'is_end': True, 'next': 0},
                'replies': [
                    {
                        'rpid': 701,
                        'oid': 303,
                        'mid': 42,
                        'root': 0,
                        'parent': 0,
                        'content': {'message': '旧自动评论'},
                    }
                ],
            },
        }

        await repository.request_scan(1)
        claim = await repository.claim_next()
        assert claim is not None and claim.part.id == 1
        await repository.complete_part(1, ())

        assert (
            await database.scalar('SELECT COUNT(*) FROM vainglory_analysis_revisions')
            == 2
        )
        assert await service.run_once() is True
        assert (
            await database.scalar('SELECT match_count FROM vainglory_publications') == 0
        )

        for _ in range(6):
            if (
                await database.scalar('SELECT state FROM vainglory_publications')
                == 'confirmed'
            ):
                break
            assert await service.run_once() is True

        assert protocol.description == '用户简介'
        assert protocol.chapter_batches[-1] == ()
        assert protocol.delete_reply_calls == [{'type': 1, 'oid': 303, 'rpid': 701}]
        assert protocol.add_reply_calls == []
        assert protocol.top_reply_calls == []
        assert (
            await database.scalar('SELECT state FROM vainglory_publications')
            == 'confirmed'
        )
        revisions = await database.fetchall(
            'SELECT reason,state,match_count FROM vainglory_publication_revisions '
            'ORDER BY revision_no'
        )
        assert [dict(row) for row in revisions] == [
            {'reason': 'initial', 'state': 'prepared', 'match_count': 1},
            {'reason': 'changed', 'state': 'confirmed', 'match_count': 0},
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unchanged_reanalysis_records_revision_without_remote_republish(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        protocol = FakePublicationProtocol()
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )
        for _ in range(6):
            assert await service.run_once() is True
        remote_calls = (
            len(protocol.edit_calls),
            len(protocol.add_reply_calls),
            len(protocol.chapter_batches),
        )
        active_revision_id = await database.scalar(
            'SELECT active_revision_id FROM vainglory_publications'
        )

        await database.execute('UPDATE vainglory_publications SET needs_refresh=1')
        assert await service.run_once() is True

        assert remote_calls == (
            len(protocol.edit_calls),
            len(protocol.add_reply_calls),
            len(protocol.chapter_batches),
        )
        assert (
            await database.scalar(
                'SELECT reason FROM vainglory_publication_revisions '
                'ORDER BY revision_no DESC LIMIT 1'
            )
            == 'unchanged'
        )
        assert (
            await database.scalar(
                'SELECT active_revision_id FROM vainglory_publications'
            )
            == active_revision_id
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_multiple_picture_comments_are_independent_root_comments(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        protocol = FakePublicationProtocol()
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )
        assert await service.run_once() is True
        publication_id = int(
            await database.scalar('SELECT id FROM vainglory_publications')
        )
        await database.execute(
            'INSERT INTO vainglory_publication_comments('
            'publication_id,ordinal,content,match_ids_json,'
            'uploaded_pictures_json,state,created_at,updated_at) '
            "VALUES(?,1,'补充分组','[1]','[]','prepared',1,1)",
            (publication_id,),
        )

        for _ in range(6):
            assert await service.run_once() is True

        assert len(protocol.add_reply_calls) == 2
        assert all('root' not in call for call in protocol.add_reply_calls)
        assert all('parent' not in call for call in protocol.add_reply_calls)
        assert all('pictures' in call for call in protocol.add_reply_calls)
        assert protocol.top_reply_calls == [
            {'type': 1, 'oid': 303, 'rpid': 501, 'action': 1}
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_definite_comment_api_error_returns_comment_to_prepared(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        protocol = FakePublicationProtocol()
        protocol.add_reply_result = BiliApiError(-400, operation='add_reply')
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        for _ in range(4):
            assert await service.run_once() is True

        assert (
            await database.scalar('SELECT state FROM vainglory_publication_comments')
            == 'prepared'
        )
        assert (
            await database.scalar('SELECT state FROM vainglory_publications')
            == 'paused'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unknown_comment_is_reconciled_without_duplicate_or_image_reupload(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        protocol = FakePublicationProtocol()
        protocol.add_reply_result = RemoteOutcomeUnknown('add_reply')
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        assert await service.run_once() is True
        assert await service.run_once() is True
        assert await service.run_once() is True
        content = protocol.add_reply_calls[0]['message']
        protocol.list_replies_result = {
            'code': 0,
            'data': {
                'replies': [
                    {
                        'rpid': 501,
                        'oid': 303,
                        'mid': 42,
                        'root': 0,
                        'content': {
                            'message': content,
                            'pictures': [{'img_src': 'result.png'}],
                        },
                    }
                ]
            },
        }

        assert await service.run_once() is True

        assert len(protocol.add_reply_calls) == 1
        assert len(protocol.picture_calls) == 1
        assert len(protocol.list_replies_calls) == 2
        assert (
            await database.scalar('SELECT state FROM vainglory_publication_comments')
            == 'confirmed'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_comment_publish_deletes_all_owned_root_comments_first(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        protocol = FakePublicationProtocol()
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        protocol.list_replies_result = {
            'code': 0,
            'data': {
                'cursor': {'is_end': True, 'next': 0},
                'replies': [
                    {
                        'rpid': 701,
                        'oid': 303,
                        'mid': 42,
                        'root': 0,
                        'parent': 0,
                        'content': {'message': '旧顶层评论'},
                    },
                    {
                        'rpid': 702,
                        'oid': 303,
                        'mid': 42,
                        'root': 701,
                        'parent': 701,
                        'content': {'message': '本账号回复'},
                    },
                    {
                        'rpid': 703,
                        'oid': 303,
                        'mid': 99,
                        'root': 0,
                        'parent': 0,
                        'content': {'message': '其他人评论'},
                    },
                ],
            },
        }

        assert await service.run_once() is True

        assert protocol.delete_reply_calls == [{'type': 1, 'oid': 303, 'rpid': 701}]
        assert protocol.add_reply_calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_service_discovers_direct_historical_archive_without_upload_job(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute('DELETE FROM upload_parts WHERE job_id=1')
        await database.execute('DELETE FROM upload_jobs WHERE id=1')
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,published_at,session_id,state,progress,'
            'page_count,completed_page_count,created_at,updated_at) '
            "VALUES(1,1,303,'BV1abcdefgh','历史稿件',1,1,'ready',1,1,1,1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_archive_parts('
            'id,import_id,page,cid,title,duration_seconds,recording_part_id,'
            'state,progress,created_at,updated_at) '
            "VALUES(1,1,1,401,'P1',600,1,'ready',1,1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_video_sources('
            'part_id,account_id,bvid,cid,page,origin,state,retention_kind,'
            'progress,original_artifact_state,created_at,updated_at) '
            "VALUES(1,1,'BV1abcdefgh',401,1,'archive','missing','analysis',"
            "0,'ready',1,1)"
        )
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True

        row = await database.fetchone(
            'SELECT source_kind,account_id,aid,bvid,session_id '
            'FROM vainglory_publications'
        )
        assert dict(row) == {
            'source_kind': 'archive',
            'account_id': 1,
            'aid': 303,
            'bvid': 'BV1abcdefgh',
            'session_id': 1,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_skipped_archive_without_video_becomes_zero_match_publication(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute('DELETE FROM vainglory_matches')
        await database.execute('DELETE FROM vainglory_analysis_revisions')
        await database.execute('DELETE FROM vainglory_part_jobs')
        await database.execute('DELETE FROM vainglory_scan_jobs')
        await database.execute('DELETE FROM upload_parts')
        await database.execute('DELETE FROM upload_jobs')
        await database.execute('DELETE FROM recording_parts')
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,published_at,session_id,state,progress,'
            'page_count,completed_page_count,content_classification,'
            'classification_reason,retryable,created_at,updated_at) '
            "VALUES(1,1,303,'BV1abcdefgh','历史稿件',1,1,'skipped',1,0,0,"
            "'unknown','稿件没有可分析的分 P',0,1,1)"
        )
        protocol = FakePublicationProtocol()
        protocol.description = (
            '用户简介\n\n共 1 局｜1 胜 0 负\n①｜胜　｜3V3｜凯恩 vs 骷髅'
        )
        protocol.chapter_cards = (
            {'from': 0, 'to': 120, 'content': '直播开始'},
            {'from': 120, 'to': 1200, 'content': '第一局｜胜｜凯恩｜3V3'},
        )
        protocol.list_replies_result = {
            'code': 0,
            'data': {
                'cursor': {'is_end': True, 'next': 0},
                'replies': [
                    {
                        'rpid': 701,
                        'oid': 303,
                        'mid': 42,
                        'root': 0,
                        'parent': 0,
                        'content': {'message': '旧自动评论'},
                    }
                ],
            },
        }
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True

        publication = await database.fetchone(
            'SELECT source_kind,plan_state,match_count,needs_refresh '
            'FROM vainglory_publications'
        )
        assert dict(publication) == {
            'source_kind': 'archive',
            'plan_state': 'ready',
            'match_count': 0,
            'needs_refresh': 0,
        }
        revision = await database.fetchone(
            'SELECT reason,state,match_count,analysis_snapshot_json '
            'FROM vainglory_publication_revisions'
        )
        assert dict(revision) == {
            'reason': 'initial',
            'state': 'prepared',
            'match_count': 0,
            'analysis_snapshot_json': (
                '{"bvid":"BV1abcdefgh","matches":[],"version":1}'
            ),
        }
        for _ in range(8):
            if (
                await database.scalar('SELECT state FROM vainglory_publications')
                == 'confirmed'
            ):
                break
            assert await service.run_once() is True
        assert protocol.description == '用户简介'
        assert protocol.chapter_batches[-1] == ()
        assert protocol.delete_reply_calls == [{'type': 1, 'oid': 303, 'rpid': 701}]
        assert protocol.add_reply_calls == []
        assert (
            await database.scalar('SELECT state FROM vainglory_publications')
            == 'confirmed'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_ready_scan_replaces_missing_legacy_upload_part_mapping(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute('DELETE FROM upload_parts WHERE job_id=1')
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True

        publication = await database.fetchone(
            'SELECT source_kind,plan_state,match_count,needs_refresh '
            'FROM vainglory_publications'
        )
        assert dict(publication) == {
            'source_kind': 'upload',
            'plan_state': 'ready',
            'match_count': 1,
            'needs_refresh': 0,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_published_upload_gets_task_before_analysis_is_ready(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute('DELETE FROM vainglory_matches')
        await database.execute(
            "UPDATE vainglory_part_jobs SET state='pending',progress=0"
        )
        await database.execute(
            "UPDATE vainglory_scan_jobs SET state='analyzing',progress=0"
        )
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True

        row = await database.fetchone(
            'SELECT source_kind,plan_state,needs_refresh,force_republish '
            'FROM vainglory_publications'
        )
        assert dict(row) == {
            'source_kind': 'upload',
            'plan_state': 'waiting_analysis',
            'needs_refresh': 1,
            'force_republish': 1,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_historical_publication_waits_for_every_archive_page(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute('DELETE FROM upload_parts WHERE job_id=1')
        await database.execute('DELETE FROM upload_jobs WHERE id=1')
        for part_id, part_index in ((2, 2), (3, 3)):
            await database.execute(
                'INSERT INTO recording_parts('
                'id,session_id,run_id,part_index,source_path,record_start_time,'
                'artifact_state,created_at,updated_at) '
                "VALUES(?,1,'run:1',?,?,1,'missing',1,1)",
                (part_id, part_index, 'bili://history/p{}'.format(part_index)),
            )
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,published_at,session_id,state,progress,'
            'page_count,completed_page_count,error,created_at,updated_at) '
            "VALUES(1,1,303,'BV1abcdefgh','历史稿件',1,1,'failed',1,3,1,"
            "'部分分 P 未完成',1,1)"
        )
        for page, part_id, state in (
            (1, 2, 'failed'),
            (2, 3, 'failed'),
            (3, 1, 'ready'),
        ):
            await database.execute(
                'INSERT INTO vainglory_archive_parts('
                'import_id,page,cid,title,duration_seconds,recording_part_id,'
                'state,progress,error,created_at,updated_at) '
                'VALUES(1,?,?,?,600,?,?,1,?,1,1)',
                (
                    page,
                    400 + page,
                    'P{}'.format(page),
                    part_id,
                    state,
                    None if state == 'ready' else '未完成',
                ),
            )
        protocol = FakePublicationProtocol()
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        assert await database.scalar('SELECT COUNT(*) FROM vainglory_publications') == 1
        assert (
            await database.scalar('SELECT plan_state FROM vainglory_publications')
            == 'waiting_analysis'
        )

        await database.execute(
            "UPDATE vainglory_archive_parts SET state='ready',progress=1,error=NULL"
        )
        await database.execute(
            "UPDATE vainglory_archive_imports SET state='ready',progress=1,"
            'completed_page_count=3,error=NULL'
        )
        assert await service.run_once() is True
        assert await database.scalar('SELECT COUNT(*) FROM vainglory_publications') == 1

        await database.execute(
            "UPDATE vainglory_archive_parts SET state='queued',progress=0 "
            'WHERE page IN (1,2)'
        )
        await database.execute(
            "UPDATE vainglory_archive_imports SET state='analyzing',progress=?,"
            'completed_page_count=1',
            (1 / 3,),
        )
        assert await service.run_once() is False
        assert protocol.edit_calls == []
        assert protocol.add_reply_calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_old_complete_hero_matches_are_publishable(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute(
            'UPDATE vainglory_matches SET hero_recognition_version=1'
        )
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        assert await database.scalar('SELECT COUNT(*) FROM vainglory_publications') == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_old_incomplete_hero_matches_publish_current_best_result(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute(
            'UPDATE vainglory_match_players SET hero_id=NULL '
            "WHERE side='left' AND slot=1"
        )
        await database.execute(
            'UPDATE vainglory_matches SET hero_recognition_version=1'
        )
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        assert await database.scalar('SELECT COUNT(*) FROM vainglory_publications') == 1
        assert (
            await database.scalar('SELECT plan_state FROM vainglory_publications')
            == 'ready'
        )
    finally:
        await database.close()


async def insert_failed_publication(database: BiliUploadDatabase) -> None:
    await database.execute(
        'INSERT INTO vainglory_publications('
        'account_id,session_id,aid,bvid,source_kind,payload_hash,'
        'description_block,state,description_state,pin_state,created_at,updated_at) '
        "VALUES(1,1,304,'BV1failedxx','upload',?,'失败灰度','failed',"
        "'confirmed','prepared',1,1)",
        ('0' * 64,),
    )


@pytest.mark.asyncio
async def test_manual_chapter_retry_preserves_other_completed_steps(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )
        assert await service.run_once() is True
        await database.execute(
            "UPDATE vainglory_publications SET state='failed',"
            "chapter_state='skipped',description_state='confirmed',"
            "pin_state='confirmed',attempt_count=5,next_attempt_at=9999,error='失败'"
        )

        await service.retry_failed_step(1, 'chapter')

        row = await database.fetchone(
            'SELECT state,chapter_state,description_state,pin_state,'
            'attempt_count,next_attempt_at,error FROM vainglory_publications'
        )
        assert dict(row) == {
            'state': 'prepared',
            'chapter_state': 'prepared',
            'description_state': 'confirmed',
            'pin_state': 'confirmed',
            'attempt_count': 0,
            'next_attempt_at': 0,
            'error': None,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_manual_pin_retry_only_resets_the_pin_step(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )
        assert await service.run_once() is True
        await database.execute(
            "UPDATE vainglory_publications SET state='failed',"
            "chapter_state='confirmed',description_state='confirmed',"
            "pin_state='in_flight',error='失败'"
        )

        await service.retry_failed_step(1, 'pin')

        row = await database.fetchone(
            'SELECT state,chapter_state,description_state,pin_state,error '
            'FROM vainglory_publications'
        )
        assert dict(row) == {
            'state': 'prepared',
            'chapter_state': 'confirmed',
            'description_state': 'confirmed',
            'pin_state': 'prepared',
            'error': None,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_manual_publication_retry_allows_resending_a_confirmed_step(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )
        assert await service.run_once() is True
        await database.execute(
            "UPDATE vainglory_publications SET state='confirmed',"
            "chapter_state='confirmed',description_state='confirmed',"
            "pin_state='confirmed',error=NULL"
        )

        await service.retry_failed_step(1, 'chapter')

        row = await database.fetchone(
            'SELECT state,chapter_state,description_state,pin_state,error '
            'FROM vainglory_publications'
        )
        assert dict(row) == {
            'state': 'prepared',
            'chapter_state': 'prepared',
            'description_state': 'confirmed',
            'pin_state': 'confirmed',
            'error': None,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_publication_does_not_block_new_discovery_for_same_account(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await insert_failed_publication(database)
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        assert await database.scalar('SELECT COUNT(*) FROM vainglory_publications') == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_publication_does_not_block_already_discovered_sibling(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        protocol = FakePublicationProtocol()
        service = VaingloryPublicationService(
            database,
            repository,
            protocol,
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )
        assert await service.run_once() is True
        await insert_failed_publication(database)

        for _ in range(2):
            assert await service.run_once() is True
            if protocol.chapter_calls:
                break
        assert protocol.chapter_calls
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_publication_uses_separate_discovery_and_delivery_workers(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        await service.start()

        assert service._discovery_task is not None
        assert service._delivery_task is not None
        assert service._discovery_task is not service._delivery_task
        assert service._discovery_task.get_name() == 'vainglory-publication-discovery'
        assert service._delivery_task.get_name() == 'vainglory-publication-delivery'
        await service.close()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_paused_migration_also_pauses_its_publication(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        repository = await seed_publication_match(database, tmp_path)
        await database.execute(
            'INSERT INTO archive_migration_jobs('
            'id,source_uid,download_account_id,target_account_id,state,progress,'
            'discovered_count,completed_count,failed_count,requested_at,'
            'updated_at,operator_paused) '
            "VALUES(1,43,1,1,'running',0,1,0,0,1,1,1)"
        )
        await database.execute(
            'INSERT INTO archive_migration_items('
            'id,migration_id,aid,bvid,title,state,progress,page_count,'
            'downloaded_page_count,session_id,upload_job_id,created_at,updated_at) '
            "VALUES(1,1,303,'BV1abcdefgh','历史稿件','task_created',1,1,1,1,1,1,1)"
        )
        service = VaingloryPublicationService(
            database,
            repository,
            FakePublicationProtocol(),
            bundle_loader=async_bundle,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await service.run_once() is True
        assert (
            await database.scalar('SELECT plan_state FROM vainglory_publications')
            == 'waiting_analysis'
        )
        status = (await service.publication_statuses((1,)))[1]
        assert status.code == 'operator_paused'
        assert status.recommended_action == 'resume_migration'
        await database.execute(
            'UPDATE archive_migration_jobs SET operator_paused=0 WHERE id=1'
        )
        assert await service.run_once() is True
    finally:
        await database.close()
