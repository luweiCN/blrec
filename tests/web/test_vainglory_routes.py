from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterator, List, Optional, Tuple
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.vainglory.archive_backfill import (
    ArchiveContentReview,
    ArchiveContentReviewPage,
    ArchiveSync,
)
from blrec.vainglory.publication import PublicationTaskStatus
from blrec.vainglory.repository import (
    AnchorStatsRecord,
    GameModeStatsRecord,
    HeroStatsRecord,
    MatchPage,
    MatchPlayerRecord,
    MatchRecord,
    MatchSessionPage,
    MatchSessionRecord,
    PlayerRecord,
    PlayerRoomRecord,
    PlayerStatsRecord,
    ZeroMatchSessionPage,
    ZeroMatchSessionRecord,
)
from blrec.web.routers import vainglory


class FakeService:
    def __init__(self, result_frame_path: Path) -> None:
        self.repository = self
        self.match_filters = {}
        self.session_filters = {}
        self.updated_titles = []
        self.updated_matches = []
        self.updated_session_titles = []
        self.updated_session_anchors = []
        self.bulk_updates = []
        self.publication_retries = []
        self.suppressed_zero_match_sessions = []
        self.restored_zero_match_sessions = []
        self.suppressed_match_reviews = []
        self.manual_match_markers = []
        self.requested_scans = []
        self.created_players = []
        self.renamed_players = []
        self.bound_rooms = []
        self.unbound_rooms = []
        self.result_frame_value = b'\x89PNG-result-frame'
        self.result_frame_path_value = result_frame_path
        self.result_frame_path_value.write_bytes(self.result_frame_value)

    async def list_matches(self, **filters: object) -> MatchPage:
        self.match_filters = filters
        return MatchPage(total=0, items=())

    async def list_match_sessions(self, **filters: object) -> MatchSessionPage:
        self.session_filters = filters
        return MatchSessionPage(
            total=1,
            items=(
                MatchSessionRecord(
                    session_id=9,
                    title='直播标题',
                    source_title='原直播标题',
                    anchor_name='主播名',
                    started_at=1_000,
                    match_count=3,
                    teal_win_count=2,
                    orange_win_count=1,
                    win_count=2,
                    loss_count=1,
                    unknown_count=0,
                    surrender_count=1,
                    duration_seconds=2_700,
                    game_modes=('3v3',),
                ),
            ),
        )

    async def list_zero_match_sessions(self, **filters: object) -> ZeroMatchSessionPage:
        self.zero_match_filters = filters
        return ZeroMatchSessionPage(
            total=1,
            items=(
                ZeroMatchSessionRecord(
                    session_id=12,
                    title='未识别直播',
                    source_title='原始标题',
                    anchor_name='待核对主播',
                    started_at=900,
                    completed_at=1_200,
                    recording_duration_seconds=7_200,
                    part_count=3,
                    bvid='BV1zero12345',
                ),
            ),
        )

    async def suppress_zero_match_session(self, session_id: int) -> None:
        self.suppressed_zero_match_sessions.append(session_id)

    async def restore_zero_match_session(self, session_id: int) -> None:
        self.restored_zero_match_sessions.append(session_id)

    async def suppress_match_review(self, match_id: int, review_type: str) -> None:
        self.suppressed_match_reviews.append((match_id, review_type))

    async def list_anchor_stats(self) -> Tuple[AnchorStatsRecord, ...]:
        return (
            AnchorStatsRecord(
                anchor_uid=42,
                anchor_name='主播名',
                room_id=100,
                session_count=8,
                match_count=20,
                win_count=12,
                loss_count=7,
                unknown_count=1,
                win_rate=0.6,
            ),
        )

    async def list_players(self) -> Tuple[PlayerRecord, ...]:
        return (stored_player(),)

    async def create_player(self, name: str) -> PlayerRecord:
        self.created_players.append(name)
        return stored_player(name=name.strip())

    async def rename_player(self, player_id: int, name: str) -> PlayerRecord:
        self.renamed_players.append((player_id, name))
        return stored_player(name=name.strip())

    async def bind_player_room(self, player_id: int, room_id: int) -> PlayerRecord:
        self.bound_rooms.append((player_id, room_id))
        return stored_player(
            rooms=(PlayerRoomRecord(room_id=room_id, anchor_uid=None, anchor_name=''),)
        )

    async def unbind_player_room(self, player_id: int, room_id: int) -> PlayerRecord:
        self.unbound_rooms.append((player_id, room_id))
        return stored_player(rooms=())

    async def list_player_stats(self) -> Tuple[PlayerStatsRecord, ...]:
        return (
            PlayerStatsRecord(
                player_id=5,
                player_name='游戏名',
                rooms=(
                    PlayerRoomRecord(room_id=100, anchor_uid=42, anchor_name='直播名'),
                ),
                session_count=8,
                match_count=20,
                win_count=12,
                loss_count=7,
                unknown_count=1,
                win_rate=0.6,
                modes=(
                    GameModeStatsRecord(
                        game_mode='3v3',
                        match_count=20,
                        win_count=12,
                        loss_count=7,
                        unknown_count=1,
                        win_rate=0.6,
                    ),
                ),
                heroes=(stored_hero_stats(),),
            ),
        )

    async def list_hero_stats(
        self, *, game_mode: str = ''
    ) -> Tuple[HeroStatsRecord, ...]:
        assert game_mode == '3v3'
        return (stored_hero_stats(),)

    async def update_match_title(self, match_id: int, title: str) -> MatchRecord:
        self.updated_titles.append((match_id, title))
        return stored_match(title=title.strip() or '投稿标题')

    async def update_match_fields(self, match_id: int, changes: object) -> MatchRecord:
        self.updated_matches.append((match_id, changes))
        return stored_match()

    async def mark_session_match(
        self, session_id: int, *, part_index: int, at_ms: int
    ) -> object:
        self.manual_match_markers.append((session_id, part_index, at_ms))
        return SimpleNamespace(
            id=8, session_id=session_id, part_id=11, part_index=part_index, at_ms=at_ms
        )

    async def request_scan(self, session_id: int) -> object:
        self.requested_scans.append(session_id)
        return object()

    async def update_session_title(
        self, session_id: int, title: str
    ) -> MatchSessionRecord:
        self.updated_session_titles.append((session_id, title))
        return MatchSessionRecord(
            session_id=session_id,
            title=title.strip() or '原直播标题',
            source_title='原直播标题',
            anchor_name='主播名',
            started_at=1_000,
            match_count=3,
            teal_win_count=2,
            orange_win_count=1,
            win_count=2,
            loss_count=1,
            unknown_count=0,
            surrender_count=1,
            duration_seconds=2_700,
            game_modes=('3v3',),
        )

    async def update_session_anchor(
        self, session_id: int, anchor_name: str
    ) -> MatchSessionRecord:
        self.updated_session_anchors.append((session_id, anchor_name))
        return MatchSessionRecord(
            session_id=session_id,
            title='直播标题',
            source_title='原直播标题',
            anchor_name=anchor_name.strip(),
            started_at=1_000,
            match_count=3,
            teal_win_count=2,
            orange_win_count=1,
            win_count=2,
            loss_count=1,
            unknown_count=0,
            surrender_count=1,
            duration_seconds=2_700,
            game_modes=('3v3',),
        )

    async def bulk_update_sessions(
        self, session_ids: List[int], **update: object
    ) -> int:
        self.bulk_updates.append((session_ids, update))
        return len(session_ids)

    async def result_frame_path(self, _match_id: int) -> Path:
        return self.result_frame_path_value

    async def retry_failed_step(self, session_id: int, step: str) -> None:
        self.publication_retries.append((session_id, step))

    async def publication_statuses(
        self, session_ids: List[int]
    ) -> Dict[int, PublicationTaskStatus]:
        assert tuple(session_ids) == (9,)
        return {
            9: PublicationTaskStatus(
                session_id=9,
                code='analysis_data_invalid',
                label='识别数据需重新分析',
                detail='缺少 OCR 对局时长',
                recommended_action='reanalyze',
                next_attempt_at=None,
                plan_state='ready',
                upload_state='approved',
                scan_state='ready',
                operator_paused=False,
            )
        }


class FakeArchiveBackfill:
    def __init__(self) -> None:
        self.requested_accounts = []
        self.requested_imports = []
        self.import_sessions: Dict[int, Optional[int]] = {}

    async def request(self, account_id: int) -> ArchiveSync:
        self.requested_accounts.append(account_id)
        return ArchiveSync(
            account_id=account_id,
            state='discovering',
            progress=0,
            discovered_count=0,
            completed_count=0,
            error=None,
            requested_at=1_000,
            started_at=None,
            completed_at=None,
            updated_at=1_000,
        )

    async def status(self, account_id: int) -> ArchiveSync:
        return ArchiveSync(
            account_id=account_id,
            state='running',
            progress=0.25,
            discovered_count=20,
            completed_count=5,
            error=None,
            requested_at=1_000,
            started_at=1_001,
            completed_at=None,
            updated_at=1_002,
        )

    async def request_import_reanalysis(self, import_id: int) -> Optional[int]:
        self.requested_imports.append(import_id)
        return self.import_sessions.get(import_id)

    async def list_suspected_non_vainglory(
        self, *, limit: int, offset: int
    ) -> ArchiveContentReviewPage:
        assert (limit, offset) == (20, 40)
        return ArchiveContentReviewPage(
            total=1,
            items=(
                ArchiveContentReview(
                    id=3,
                    account_id=7,
                    account_name='投稿账号',
                    aid=123,
                    bvid='BV1abcdefgh',
                    title='其他游戏直播',
                    published_at=900,
                    reason='所有分P分析完成，但未发现虚荣对局结算',
                ),
            ),
        )


def stored_match(*, title: str = '投稿标题') -> MatchRecord:
    return MatchRecord(
        id=3,
        session_id=9,
        session_title='直播标题',
        session_started_at=1_000,
        part_id=11,
        part_index=2,
        title=title,
        source_title='直播标题',
        upload_title='投稿标题',
        game_mode='3v3',
        team_size=3,
        started_at_ms=60_000,
        result_at_ms=960_000,
        duration_seconds=900,
        result_text='获胜',
        end_reason='normal',
        left_color='teal',
        right_color='orange',
        winner_side='left',
        winner_color='teal',
        left_kills=20,
        right_kills=10,
        left_economy=40_000,
        right_economy=30_000,
        confidence=0.98,
        account_id=7,
        bvid='BV1abcdefgh',
        archive_page=2,
        has_result_frame=True,
        recorded_player_confidence=None,
        recorded_player_source='automatic',
        players=(
            MatchPlayerRecord(
                side='left',
                slot=1,
                name='玩家',
                normalized_name='玩家',
                hero_id=5,
                hero_label='英雄',
                hero_source='automatic',
                kills=7,
                deaths=2,
                assists=8,
                economy=15_000,
                confidence=0.9,
            ),
        ),
    )


def stored_player(
    *,
    name: str = '游戏名',
    rooms: Tuple[PlayerRoomRecord, ...] = (
        PlayerRoomRecord(room_id=100, anchor_uid=42, anchor_name='直播名'),
    ),
) -> PlayerRecord:
    return PlayerRecord(
        id=5,
        name=name,
        origin='manual',
        rooms=rooms,
        created_at=1_000,
        updated_at=1_001,
    )


def stored_hero_stats() -> HeroStatsRecord:
    return HeroStatsRecord(
        hero_id=7,
        hero_label='凯恩',
        player_count=1,
        match_count=10,
        win_count=6,
        loss_count=4,
        unknown_count=0,
        win_rate=0.6,
    )


@pytest.fixture
def api_client(tmp_path: Path) -> Iterator[Tuple[TestClient, FakeService]]:
    application = FastAPI()
    fake = FakeService(tmp_path / 'result-frame.png')
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[vainglory.authenticated_manager_subject] = (
        lambda: 'manager'
    )
    application.dependency_overrides[vainglory.get_service] = lambda: fake
    application.dependency_overrides[vainglory.get_publication] = lambda: fake
    with TestClient(application) as client:
        yield client, fake


def test_retries_or_resends_each_publication_step_independently(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    description = client.post(
        '/api/v1/vainglory/sessions/9/publication/description/retry'
    )
    comments = client.post('/api/v1/vainglory/sessions/9/publication/comments/retry')
    pin = client.post('/api/v1/vainglory/sessions/9/publication/pin/retry')
    chapter = client.post('/api/v1/vainglory/sessions/9/publication/chapter/retry')

    assert description.status_code == 202
    assert comments.status_code == 202
    assert pin.status_code == 202
    assert chapter.status_code == 202
    assert fake.publication_retries == [
        (9, 'description'),
        (9, 'comments'),
        (9, 'pin'),
        (9, 'chapter'),
    ]


def test_match_filters_use_camel_case_query_names(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client
    response = client.get(
        '/api/v1/vainglory/matches',
        params=[
            ('playerName', '5555-2'),
            ('heroId', '7'),
            ('heroId', '8'),
            ('winnerColor', 'teal'),
            ('endReason', 'surrender'),
            ('gameMode', '3v3'),
            ('sessionId', '9'),
        ],
    )

    assert response.status_code == 200
    assert fake.match_filters == {
        'player_name': '5555-2',
        'hero_ids': [7, 8],
        'winner_color': 'teal',
        'end_reason': 'surrender',
        'game_mode': '3v3',
        'session_id': 9,
        'limit': 50,
        'offset': 0,
    }


def test_lists_recording_session_summaries_instead_of_flat_matches(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client
    response = client.get(
        '/api/v1/vainglory/sessions',
        params={'playerName': '玩家', 'limit': 20, 'offset': 40},
    )

    assert response.status_code == 200
    assert fake.session_filters['player_name'] == '玩家'
    assert fake.session_filters['limit'] == 20
    assert fake.session_filters['offset'] == 40
    payload = response.json()
    assert payload['total'] == 1
    assert payload['items'][0]['sessionId'] == 9
    assert payload['items'][0]['matchCount'] == 3
    assert payload['items'][0]['tealWinCount'] == 2
    assert payload['items'][0]['winCount'] == 2
    assert payload['items'][0]['lossCount'] == 1
    assert payload['items'][0]['anchorName'] == '主播名'
    assert payload['items'][0]['publicationStatus'] == 'analysis_data_invalid'
    assert payload['items'][0]['publicationStatusLabel'] == '识别数据需重新分析'
    assert payload['items'][0]['publicationStatusDetail'] == '缺少 OCR 对局时长'
    assert payload['items'][0]['publicationRecommendedAction'] == 'reanalyze'
    assert payload['items'][0]['publicationPlanState'] == 'ready'
    assert payload['items'][0]['uploadJobState'] == 'approved'
    assert payload['items'][0]['publicationScanState'] == 'ready'
    assert payload['items'][0]['publicationOperatorPaused'] is False


def test_lists_completed_zero_match_sessions_for_review(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    response = client.get(
        '/api/v1/vainglory/zero-match-sessions', params={'limit': 10, 'offset': 20}
    )

    assert response.status_code == 200
    assert fake.zero_match_filters == {'limit': 10, 'offset': 20, 'suppressed': False}
    assert response.json() == {
        'total': 1,
        'items': [
            {
                'sessionId': 12,
                'title': '未识别直播',
                'sourceTitle': '原始标题',
                'anchorName': '待核对主播',
                'startedAt': 900,
                'completedAt': 1_200,
                'recordingDurationSeconds': 7_200,
                'partCount': 3,
                'bvid': 'BV1zero12345',
            }
        ],
    }


def test_lists_and_updates_confirmed_zero_match_scan_suppressions(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    listed = client.get(
        '/api/v1/vainglory/zero-match-sessions', params={'suppressed': 'true'}
    )
    suppressed = client.put('/api/v1/vainglory/sessions/12/scan-suppression')
    restored = client.delete('/api/v1/vainglory/sessions/12/scan-suppression')

    assert listed.status_code == 200
    assert fake.zero_match_filters == {'limit': 20, 'offset': 0, 'suppressed': True}
    assert suppressed.status_code == 204
    assert restored.status_code == 204
    assert fake.suppressed_zero_match_sessions == [12]
    assert fake.restored_zero_match_sessions == [12]


def test_zero_match_session_can_add_a_manual_match_marker(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    response = client.post(
        '/api/v1/vainglory/sessions/12/match-markers',
        json={'partIndex': 2, 'atMs': 754_000},
    )

    assert response.status_code == 201
    assert response.json() == {
        'id': 8,
        'sessionId': 12,
        'partId': 11,
        'partIndex': 2,
        'atMs': 754_000,
    }
    assert fake.manual_match_markers == [(12, 2, 754_000)]


def test_lists_anchor_win_loss_statistics(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, _fake = api_client

    response = client.get('/api/v1/vainglory/stats/anchors')

    assert response.status_code == 200
    assert response.json() == [
        {
            'anchorUid': 42,
            'anchorName': '主播名',
            'roomId': 100,
            'sessionCount': 8,
            'matchCount': 20,
            'winCount': 12,
            'lossCount': 7,
            'unknownCount': 1,
            'winRate': 0.6,
        }
    ]


def test_manages_player_library_and_lists_player_rankings(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    listed = client.get('/api/v1/vainglory/players')
    created = client.post('/api/v1/vainglory/players', json={'name': ' 新玩家 '})
    renamed = client.patch('/api/v1/vainglory/players/5', json={'name': ' 新名字 '})
    bound = client.put('/api/v1/vainglory/players/5/rooms/200')
    unbound = client.delete('/api/v1/vainglory/players/5/rooms/200')
    player_stats = client.get('/api/v1/vainglory/stats/players')
    hero_stats = client.get(
        '/api/v1/vainglory/stats/heroes', params={'gameMode': '3v3'}
    )

    assert listed.status_code == 200
    assert listed.json()[0]['name'] == '游戏名'
    assert listed.json()[0]['rooms'][0] == {
        'roomId': 100,
        'anchorUid': 42,
        'anchorName': '直播名',
    }
    assert created.status_code == 201
    assert created.json()['name'] == '新玩家'
    assert renamed.status_code == 200
    assert renamed.json()['name'] == '新名字'
    assert bound.status_code == 200
    assert bound.json()['rooms'][0]['roomId'] == 200
    assert unbound.status_code == 200
    assert unbound.json()['rooms'] == []
    assert fake.created_players == [' 新玩家 ']
    assert fake.renamed_players == [(5, ' 新名字 ')]
    assert fake.bound_rooms == [(5, 200)]
    assert fake.unbound_rooms == [(5, 200)]
    assert player_stats.status_code == 200
    assert player_stats.json()[0]['playerName'] == '游戏名'
    assert player_stats.json()[0]['modes'][0]['gameMode'] == '3v3'
    assert player_stats.json()[0]['heroes'][0]['heroLabel'] == '凯恩'
    assert hero_stats.status_code == 200
    assert hero_stats.json()[0]['playerCount'] == 1


def test_updates_one_title_for_the_whole_recording_session(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    response = client.patch(
        '/api/v1/vainglory/sessions/9', json={'title': '  整场标题  '}
    )

    assert response.status_code == 200
    assert fake.updated_session_titles == [(9, '  整场标题  ')]
    assert response.json()['title'] == '整场标题'
    assert response.json()['sourceTitle'] == '原直播标题'


def test_updates_anchor_and_bulk_statistics_membership(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    anchor = client.patch(
        '/api/v1/vainglory/sessions/9/anchor', json={'anchorName': '  玩不明白  '}
    )
    bulk = client.patch(
        '/api/v1/vainglory/sessions/bulk-update',
        json={'sessionIds': [9, 10], 'statsIncluded': False},
    )

    assert anchor.status_code == 200
    assert anchor.json()['anchorName'] == '玩不明白'
    assert fake.updated_session_anchors == [(9, '  玩不明白  ')]
    assert bulk.status_code == 200
    assert bulk.json() == {'updatedCount': 2}
    assert fake.bulk_updates == [
        ([9, 10], {'anchor_name': None, 'stats_included': False})
    ]


def test_updates_match_title_and_returns_timeline_metadata(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client
    response = client.patch(
        '/api/v1/vainglory/matches/3', json={'title': '  单独标题  '}
    )

    assert response.status_code == 200
    assert fake.updated_titles == [(3, '  单独标题  ')]
    payload = response.json()
    assert payload['title'] == '单独标题'
    assert payload['sourceTitle'] == '直播标题'
    assert payload['uploadTitle'] == '投稿标题'
    assert payload['gameMode'] == '3v3'
    assert payload['matchKind'] == 'unknown'
    assert payload['viewContext'] == 'unknown'
    assert payload['statsEligible'] is True
    assert payload['statsExclusionReason'] is None
    assert payload['startedAtMs'] == 60_000
    assert payload['bvid'] == 'BV1abcdefgh'
    assert payload['archivePage'] == 2
    assert payload['resultFrameUrl'] == (
        '/api/v1/vainglory/matches/3/result-frame?v=9-11-960000'
    )


def test_suppresses_one_review_queue_without_deleting_the_match(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    response = client.put(
        '/api/v1/vainglory/matches/3/review-suppressions/recorded_player'
    )

    assert response.status_code == 204
    assert fake.suppressed_match_reviews == [(3, 'recorded_player')]


def test_updates_all_editable_match_fields(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    response = client.patch(
        '/api/v1/vainglory/matches/3',
        json={
            'gameMode': '5v5',
            'winnerColor': 'orange',
            'statsEligible': False,
            'players': [
                {'side': 'left', 'slot': 1, 'name': '修正玩家', 'heroId': 7, 'kills': 8}
            ],
        },
    )

    assert response.status_code == 200
    assert fake.updated_matches == [
        (
            3,
            {
                'game_mode': '5v5',
                'winner_color': 'orange',
                'stats_eligible': False,
                'players': [
                    {
                        'side': 'left',
                        'slot': 1,
                        'name': '修正玩家',
                        'hero_id': 7,
                        'kills': 8,
                    }
                ],
            },
        )
    ]


def test_serves_result_frame_inline_or_as_download(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    inline = client.get('/api/v1/vainglory/matches/3/result-frame')
    download = client.get(
        '/api/v1/vainglory/matches/3/result-frame', params={'download': 'true'}
    )

    assert inline.status_code == 200
    assert inline.content == fake.result_frame_value
    assert inline.headers['content-type'] == 'image/png'
    assert inline.headers['cache-control'] == 'private, no-store'
    assert inline.headers['content-disposition'].startswith('inline;')
    assert download.headers['content-disposition'].startswith('attachment;')


def test_screenshot_recognition_endpoint_is_removed(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, _fake = api_client
    response = client.post(
        '/api/v1/vainglory/recognize-screenshot',
        headers={'content-type': 'image/png'},
        content=b'image',
    )

    assert response.status_code == 404


def test_requests_and_reads_account_archive_backfill() -> None:
    application = FastAPI()
    fake = FakeArchiveBackfill()
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[vainglory.authenticated_manager_subject] = (
        lambda: 'manager'
    )
    application.dependency_overrides[vainglory.get_archive_backfill] = lambda: fake

    with TestClient(application) as client:
        requested = client.post('/api/v1/vainglory/archive-syncs/7')
        status_response = client.get('/api/v1/vainglory/archive-syncs/7')

    assert requested.status_code == 202
    assert requested.json()['state'] == 'discovering'
    assert fake.requested_accounts == [7]
    assert status_response.status_code == 200
    assert status_response.json()['progress'] == 0.25
    assert status_response.json()['discoveredCount'] == 20


def test_requests_archive_import_reanalysis_and_delegates_existing_session(
    tmp_path: Path,
) -> None:
    application = FastAPI()
    service = FakeService(tmp_path / 'result.png')
    backfill = FakeArchiveBackfill()
    backfill.import_sessions[8] = 9
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[vainglory.authenticated_manager_subject] = (
        lambda: 'manager'
    )
    application.dependency_overrides[vainglory.get_service] = lambda: service
    application.dependency_overrides[vainglory.get_archive_backfill] = lambda: backfill

    with TestClient(application) as client:
        unmaterialized = client.post('/api/v1/vainglory/archive-imports/7/scan')
        materialized = client.post('/api/v1/vainglory/archive-imports/8/scan')

    assert unmaterialized.status_code == 202
    assert materialized.status_code == 202
    assert backfill.requested_imports == [7, 8]
    assert service.requested_scans == [9]


def test_lists_suspected_non_vainglory_public_archives() -> None:
    application = FastAPI()
    fake = FakeArchiveBackfill()
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[vainglory.authenticated_manager_subject] = (
        lambda: 'manager'
    )
    application.dependency_overrides[vainglory.get_archive_backfill] = lambda: fake

    with TestClient(application) as client:
        response = client.get(
            '/api/v1/vainglory/archive-content-reviews',
            params={'limit': 20, 'offset': 40},
        )

    assert response.status_code == 200
    assert response.json()['total'] == 1
    assert response.json()['items'][0]['bvid'] == 'BV1abcdefgh'


def test_manages_analysis_workers() -> None:
    worker = SimpleNamespace(
        state='running',
        worker_id='mac-studio',
        display_name='Mac Studio',
        enabled=True,
        model_package_id='vg-vision-v2',
        pipeline_version='timeline-v2',
        last_seen_at=1_000,
        active_task_count=1,
        active_part_ids=(7,),
        concurrency=3,
        completed_task_count=12,
        failed_task_count=1,
        total_processing_seconds=240.0,
        profiled_task_count=10,
        profiled_video_seconds=18_000.0,
        total_decode_analysis_seconds=600.0,
        total_profiled_task_seconds=900.0,
        last_task_finished_at=999,
    )
    service = SimpleNamespace()
    service.list_analysis_workers = AsyncMock(return_value=(worker,))
    service.add_analysis_worker = AsyncMock(return_value=worker)
    paused = SimpleNamespace(**{**worker.__dict__, 'enabled': False})
    service.update_analysis_worker = AsyncMock(return_value=paused)
    application = FastAPI()
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[vainglory.authenticated_manager_subject] = (
        lambda: 'manager'
    )
    application.dependency_overrides[vainglory.get_service] = lambda: service

    with TestClient(application) as client:
        listed = client.get('/api/v1/vainglory/workers')
        created = client.post(
            '/api/v1/vainglory/workers',
            json={'workerId': 'mac-studio', 'displayName': ' Mac Studio '},
        )
        updated = client.patch(
            '/api/v1/vainglory/workers/mac-studio', json={'enabled': False}
        )
        invalid = client.post(
            '/api/v1/vainglory/workers', json={'workerId': '../not-allowed'}
        )

    assert listed.status_code == 200
    assert listed.json()['workers'][0]['activePartIds'] == [7]
    assert listed.json()['workers'][0]['profiledTaskCount'] == 10
    assert listed.json()['workers'][0]['profiledVideoSeconds'] == 18_000.0
    assert listed.json()['workers'][0]['totalDecodeAnalysisSeconds'] == 600.0
    assert listed.json()['workers'][0]['totalProfiledTaskSeconds'] == 900.0
    assert created.status_code == 201
    assert updated.status_code == 200
    assert updated.json()['enabled'] is False
    assert invalid.status_code == 422
    service.add_analysis_worker.assert_awaited_once_with('mac-studio', 'Mac Studio')
    service.update_analysis_worker.assert_awaited_once_with(
        'mac-studio', display_name=None, enabled=False
    )


def test_worker_completion_forwards_efficiency_metrics() -> None:
    service = SimpleNamespace()
    service.register_remote_worker_activity = Mock()
    service.complete_remote_part = AsyncMock()
    application = FastAPI()
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[
        vainglory.security.authenticated_analysis_worker
    ] = lambda: 'analysis-worker'
    application.dependency_overrides[vainglory.get_service] = lambda: service

    with TestClient(application) as client:
        response = client.post(
            '/api/v1/vainglory/worker/complete',
            json={
                'workerId': 'mac-studio',
                'modelPackageId': 'vg-vision-v2',
                'pipelineVersion': 'timeline-v2',
                'concurrency': 3,
                'kind': 'part',
                'itemId': 7,
                'videoDurationSeconds': 3_600,
                'decodeAnalysisSeconds': 120,
            },
        )

    assert response.status_code == 204
    service.complete_remote_part.assert_awaited_once_with(
        7,
        (),
        candidate_count=0,
        training_candidates=(),
        analysis_summary=None,
        video_duration_seconds=3_600,
        decode_analysis_seconds=120,
    )
