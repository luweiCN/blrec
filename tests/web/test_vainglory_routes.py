from pathlib import Path
from typing import Iterator, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.vainglory.archive_backfill import (
    ArchiveContentReview,
    ArchiveContentReviewPage,
    ArchiveSync,
)
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
)
from blrec.web.routers import vainglory


class FakeService:
    def __init__(self, result_frame_path: Path) -> None:
        self.repository = self
        self.match_filters = {}
        self.session_filters = {}
        self.updated_titles = []
        self.updated_session_titles = []
        self.updated_session_anchors = []
        self.bulk_updates = []
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


class FakeArchiveBackfill:
    def __init__(self) -> None:
        self.requested_accounts = []

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
    with TestClient(application) as client:
        yield client, fake


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
    assert payload['startedAtMs'] == 60_000
    assert payload['bvid'] == 'BV1abcdefgh'
    assert payload['archivePage'] == 2
    assert payload['resultFrameUrl'] == (
        '/api/v1/vainglory/matches/3/result-frame?v=9-11-960000'
    )


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
