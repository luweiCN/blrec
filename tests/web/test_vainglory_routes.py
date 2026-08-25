from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterator, List, Optional, Tuple
from unittest.mock import AsyncMock, Mock, call

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.remote_media import (
    RemoteMediaQueueItem,
    RemoteMediaQueuePage,
    RemoteMediaQueueStatus,
)
from blrec.vainglory.analyzer import VideoPart
from blrec.vainglory.archive_backfill import (
    ArchiveBackfillItem,
    ArchiveContentReview,
    ArchiveContentReviewPage,
    ArchiveSync,
)
from blrec.vainglory.publication import (
    PublicationAuditStatus,
    PublicationRecord,
    PublicationRecordPage,
    PublicationTaskStatus,
)
from blrec.vainglory.repository import (
    AnalysisQueueItem,
    AnalysisQueueStatus,
    AnchorStatsRecord,
    GameModeStatsRecord,
    HeroStatsRecord,
    LiveAnalysisClaim,
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
from blrec.vainglory.service import RemoteAnalysisClaim
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
        self.publication_audit_requests = []
        self.publication_record_requests = []
        self.suppressed_zero_match_sessions = []
        self.restored_zero_match_sessions = []
        self.suppressed_match_reviews = []
        self.duplicate_reviews = []
        self.manual_match_markers = []
        self.requested_scans = []
        self.created_players = []
        self.renamed_players = []
        self.player_visibility_updates = []
        self.bound_aliases = []
        self.bound_rooms = []
        self.unbound_rooms = []
        self.result_frame_value = b'\x89PNG-result-frame'
        self.result_frame_path_value = result_frame_path
        self.result_frame_path_value.write_bytes(self.result_frame_value)

    async def list_matches(self, **filters: object) -> MatchPage:
        self.match_filters = filters
        return MatchPage(total=0, items=())

    async def list_duplicate_reviews(self, **filters: object) -> MatchPage:
        return MatchPage(
            total=1,
            items=(
                replace(
                    stored_match(),
                    stats_eligible=False,
                    stats_exclusion_reason='duplicate',
                    duplicate_of_match_id=2,
                    duplicate_session_id=1,
                    duplicate_anchor_name='原主播',
                    duplicate_review_state='pending',
                ),
            ),
        )

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

    async def set_player_public_visibility(
        self, player_id: int, public_visible: bool
    ) -> PlayerRecord:
        self.player_visibility_updates.append((player_id, public_visible))
        return stored_player(public_visible=public_visible)

    async def bind_player_alias(self, player_id: int, alias: str) -> PlayerRecord:
        self.bound_aliases.append((player_id, alias))
        return stored_player()

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

    async def review_match_duplicate(
        self,
        match_id: int,
        *,
        confirmed: bool,
        canonical_anchor_name: str | None = None,
    ) -> MatchRecord:
        self.duplicate_reviews.append((match_id, confirmed, canonical_anchor_name))
        return replace(
            stored_match(),
            stats_eligible=not confirmed,
            stats_exclusion_reason='duplicate' if confirmed else None,
            duplicate_of_match_id=2 if confirmed else None,
            duplicate_review_state='confirmed' if confirmed else 'dismissed',
        )

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

    async def publication_audit_status(
        self, *, stale_before: int
    ) -> PublicationAuditStatus:
        self.publication_audit_requests.append(('status', stale_before))
        return PublicationAuditStatus(
            total_count=30,
            verified_count=24,
            stale_count=4,
            pending_count=5,
            failed_count=1,
            oldest_verified_at=1_000,
        )

    async def queue_publication_audit(self, *, stale_before: int, limit: int) -> int:
        self.publication_audit_requests.append(('queue', stale_before, limit))
        return min(4, limit)

    async def list_publication_records(
        self, *, status: str, limit: int, offset: int
    ) -> PublicationRecordPage:
        self.publication_record_requests.append(('list', status, limit, offset))
        task_status = PublicationTaskStatus(
            session_id=9,
            code='failed',
            label='发布任务失败',
            detail='需要人工处理',
            recommended_action='retry',
            next_attempt_at=None,
            plan_state='ready',
            upload_state='approved',
            scan_state='ready',
            operator_paused=False,
        )
        return PublicationRecordPage(
            total=1,
            items=(
                PublicationRecord(
                    id=7,
                    session_id=9,
                    bvid='BV1abcdefgh',
                    title='直播回放',
                    source_kind='archive',
                    state='failed',
                    visibility_scope='owner',
                    match_count=3,
                    updated_at=1_000,
                    remote_verified_at=None,
                    status=task_status,
                ),
            ),
        )

    async def retry_publication(self, publication_id: int) -> None:
        self.publication_record_requests.append(('retry', publication_id))


class FakeArchiveBackfill:
    def __init__(self) -> None:
        self.requested_accounts = []
        self.requested_imports = []
        self.import_sessions: Dict[int, Optional[int]] = {}
        self.item_requests = []
        self.control_updates = []

    async def request(self, account_id: int, *, rescan: bool = False) -> ArchiveSync:
        self.requested_accounts.append((account_id, rescan))
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

    async def update_control(
        self,
        account_id: int,
        *,
        paused: Optional[bool] = None,
        daily_limit: Optional[int] = None,
    ) -> ArchiveSync:
        self.control_updates.append((account_id, paused, daily_limit))
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
            daily_limit=daily_limit or 20,
        )

    async def request_import_reanalysis(self, import_id: int) -> Optional[int]:
        self.requested_imports.append(import_id)
        return self.import_sessions.get(import_id)

    async def count_items(self, account_id: int) -> int:
        assert account_id == 7
        return 21

    async def list_items(
        self, account_id: int, *, limit: int = 30, offset: int = 0
    ) -> tuple:
        self.item_requests.append((account_id, limit, offset))
        return (
            ArchiveBackfillItem(
                id=3,
                account_id=account_id,
                aid=123,
                bvid='BV1abcdefgh',
                title='历史直播',
                published_at=900,
                state='queued',
                stage='analysis_pending',
                progress=0.5,
                page_count=1,
                completed_page_count=1,
                current_page=1,
                current_part_title='P1',
                download_progress=1,
                downloaded_bytes=100,
                total_bytes=100,
                analysis_state='pending',
                analysis_progress=0,
                match_count=0,
                publication_state=None,
                description_state=None,
                comment_count=0,
                confirmed_comment_count=0,
                pin_state=None,
                publication_progress=0,
                error=None,
                updated_at=1_002,
            ),
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
    public_visible: bool = True,
    rooms: Tuple[PlayerRoomRecord, ...] = (
        PlayerRoomRecord(room_id=100, anchor_uid=42, anchor_name='直播名'),
    ),
) -> PlayerRecord:
    return PlayerRecord(
        id=5,
        name=name,
        origin='manual',
        public_visible=public_visible,
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


def test_reads_and_queues_low_priority_publication_audits(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    current = client.get(
        '/api/v1/vainglory/publication-audits', params={'maxAgeHours': 24}
    )
    queued = client.post(
        '/api/v1/vainglory/publication-audits', json={'maxAgeHours': 24, 'limit': 3}
    )

    assert current.status_code == 200
    assert current.json()['verifiedCount'] == 24
    assert current.json()['staleCount'] == 4
    assert queued.status_code == 202
    assert queued.json()['queuedCount'] == 3
    assert queued.json()['pendingCount'] == 5
    assert fake.publication_audit_requests[0][0] == 'status'
    assert fake.publication_audit_requests[1][0] == 'queue'
    assert fake.publication_audit_requests[1][2] == 3
    assert fake.publication_audit_requests[2][0] == 'status'


def test_lists_and_retries_publication_records(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    listed = client.get(
        '/api/v1/vainglory/publication-records',
        params={'status': 'needs_action', 'limit': 20, 'offset': 0},
    )
    retried = client.post('/api/v1/vainglory/publication-records/7/retry')

    assert listed.status_code == 200
    assert listed.json()['total'] == 1
    assert listed.json()['items'][0]['bvid'] == 'BV1abcdefgh'
    assert listed.json()['items'][0]['statusCode'] == 'failed'
    assert retried.status_code == 202
    assert fake.publication_record_requests == [
        ('list', 'needs_action', 20, 0),
        ('retry', 7),
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
    hidden = client.patch(
        '/api/v1/vainglory/players/5/visibility', json={'publicVisible': False}
    )
    aliased = client.put(
        '/api/v1/vainglory/players/5/aliases', json={'name': ' 旧名字 '}
    )
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
    assert hidden.status_code == 200
    assert hidden.json()['publicVisible'] is False
    assert aliased.status_code == 200
    assert bound.status_code == 200
    assert bound.json()['rooms'][0]['roomId'] == 200
    assert unbound.status_code == 200
    assert unbound.json()['rooms'] == []
    assert fake.created_players == [' 新玩家 ']
    assert fake.renamed_players == [(5, ' 新名字 ')]
    assert fake.player_visibility_updates == [(5, False)]
    assert fake.bound_aliases == [(5, ' 旧名字 ')]
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
    assert payload['duplicateOfMatchId'] is None
    assert payload['duplicateReviewState'] == 'none'
    assert payload['startedAtMs'] == 60_000
    assert payload['bvid'] == 'BV1abcdefgh'
    assert payload['archivePage'] == 2
    assert payload['resultFrameUrl'] == (
        '/api/v1/vainglory/matches/3/result-frame?v=9-11-960000'
    )


def test_reviews_a_suspected_duplicate(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, fake = api_client

    response = client.put(
        '/api/v1/vainglory/matches/3/duplicate-review',
        json={'decision': 'confirmed', 'canonicalAnchorName': '真实主播'},
    )

    assert response.status_code == 200
    assert fake.duplicate_reviews == [(3, True, '真实主播')]
    assert response.json()['duplicateReviewState'] == 'confirmed'
    assert response.json()['duplicateOfMatchId'] == 2


def test_lists_suspected_duplicates_for_review(
    api_client: Tuple[TestClient, FakeService]
) -> None:
    client, _fake = api_client

    response = client.get('/api/v1/vainglory/duplicate-reviews')

    assert response.status_code == 200
    assert response.json()['total'] == 1
    assert response.json()['items'][0]['duplicateReviewState'] == 'pending'
    assert response.json()['items'][0]['duplicateSessionId'] == 1
    assert response.json()['items'][0]['duplicateAnchorName'] == '原主播'


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
        rescanned = client.post(
            '/api/v1/vainglory/archive-syncs/7', json={'rescan': True}
        )
        status_response = client.get('/api/v1/vainglory/archive-syncs/7')
        updated = client.patch(
            '/api/v1/vainglory/archive-syncs/7', json={'dailyLimit': 50_000}
        )
        item_page = client.get(
            '/api/v1/vainglory/archive-syncs/7/item-page',
            params={'limit': 20, 'offset': 20},
        )

    assert requested.status_code == 202
    assert requested.json()['state'] == 'discovering'
    assert fake.requested_accounts == [(7, False), (7, True)]
    assert rescanned.status_code == 202
    assert status_response.status_code == 200
    assert status_response.json()['progress'] == 0.25
    assert status_response.json()['discoveredCount'] == 20
    assert updated.status_code == 200
    assert updated.json()['dailyLimit'] == 50_000
    assert fake.control_updates == [(7, None, 50_000)]
    assert item_page.status_code == 200
    assert item_page.json()['total'] == 21
    assert item_page.json()['items'][0]['bvid'] == 'BV1abcdefgh'
    assert fake.item_requests == [(7, 20, 20)]


def test_reads_and_updates_remote_media_download_queue() -> None:
    cache = SimpleNamespace()
    cache.queue_status = AsyncMock(
        return_value=RemoteMediaQueueStatus(
            pending_download_count=2_385,
            pending_download_archive_count=1_420,
            active_download_count=6,
            active_download_archive_count=3,
            downloaded_waiting_analysis_count=243,
            downloaded_waiting_analysis_archive_count=172,
            active_analysis_count=3,
            active_analysis_archive_count=2,
            failed_download_count=86,
            failed_download_archive_count=61,
            downloads_per_interface=3,
            interface_count=2,
            total_concurrency=6,
            latest_activity_at=1_000,
        )
    )
    cache.update_downloads_per_interface = AsyncMock(
        return_value=RemoteMediaQueueStatus(
            pending_download_count=2_385,
            pending_download_archive_count=1_420,
            active_download_count=6,
            active_download_archive_count=3,
            downloaded_waiting_analysis_count=243,
            downloaded_waiting_analysis_archive_count=172,
            active_analysis_count=3,
            active_analysis_archive_count=2,
            failed_download_count=86,
            failed_download_archive_count=61,
            downloads_per_interface=4,
            interface_count=2,
            total_concurrency=8,
            latest_activity_at=1_001,
        )
    )
    application = FastAPI()
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[vainglory.authenticated_manager_subject] = (
        lambda: 'manager'
    )
    application.dependency_overrides[vainglory.get_remote_media_cache] = lambda: cache

    with TestClient(application) as client:
        current = client.get('/api/v1/vainglory/archive-download-queue')
        updated = client.patch(
            '/api/v1/vainglory/archive-download-queue',
            json={'downloadsPerInterface': 4},
        )

    assert current.status_code == 200
    assert current.json() == {
        'pendingDownloadCount': 2_385,
        'pendingDownloadArchiveCount': 1_420,
        'activeDownloadCount': 6,
        'activeDownloadArchiveCount': 3,
        'downloadedWaitingAnalysisCount': 243,
        'downloadedWaitingAnalysisArchiveCount': 172,
        'activeAnalysisCount': 3,
        'activeAnalysisArchiveCount': 2,
        'failedDownloadCount': 86,
        'failedDownloadArchiveCount': 61,
        'downloadsPerInterface': 3,
        'interfaceCount': 2,
        'totalConcurrency': 6,
        'latestActivityAt': 1_000,
    }
    assert updated.status_code == 200
    assert updated.json()['downloadsPerInterface'] == 4
    assert updated.json()['totalConcurrency'] == 8
    cache.queue_status.assert_awaited_once_with()
    cache.update_downloads_per_interface.assert_awaited_once_with(4)


def test_lists_and_retries_remote_media_download_items(monkeypatch) -> None:
    item = RemoteMediaQueueItem(
        part_id=9,
        archive_import_id=4,
        account_id=2,
        account_name='历史账号',
        bvid='BV1abcdefgh',
        archive_title='直播回放',
        page=2,
        page_count=3,
        part_title='P2',
        queue_state='failed',
        source_state='failed',
        analysis_state=None,
        progress=0,
        downloaded_bytes=128,
        total_bytes=1_024,
        speed_bytes_per_second=None,
        error='下载中断',
        updated_at=1_000,
    )
    status = RemoteMediaQueueStatus(
        pending_download_count=1,
        pending_download_archive_count=1,
        active_download_count=0,
        active_download_archive_count=0,
        downloaded_waiting_analysis_count=0,
        downloaded_waiting_analysis_archive_count=0,
        active_analysis_count=0,
        active_analysis_archive_count=0,
        failed_download_count=0,
        failed_download_archive_count=0,
        downloads_per_interface=1,
        interface_count=1,
        total_concurrency=1,
        latest_activity_at=1_000,
    )
    cache = SimpleNamespace(
        queue_items=AsyncMock(
            return_value=RemoteMediaQueuePage(total=1, archive_count=1, items=(item,))
        ),
        failed_part_ids=AsyncMock(return_value=(9, 10)),
        request=AsyncMock(),
        queue_status=AsyncMock(return_value=status),
    )
    backfill = SimpleNamespace(retry_download_part=AsyncMock(return_value=True))
    monkeypatch.setattr(vainglory, 'archive_backfill', backfill)
    application = FastAPI()
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[vainglory.authenticated_manager_subject] = (
        lambda: 'manager'
    )
    application.dependency_overrides[vainglory.get_remote_media_cache] = lambda: cache

    with TestClient(application) as client:
        listed = client.get(
            '/api/v1/vainglory/archive-download-queue/items',
            params={'queue_state': 'failed', 'limit': 30, 'offset': 0},
        )
        retried = client.post('/api/v1/vainglory/archive-download-queue/items/9/retry')
        retried_all = client.post(
            '/api/v1/vainglory/archive-download-queue/retry-failed'
        )

    assert listed.status_code == 200
    assert listed.json()['archiveCount'] == 1
    assert listed.json()['items'][0]['error'] == '下载中断'
    assert retried.status_code == 200
    assert retried_all.status_code == 200
    assert retried_all.json()['retriedCount'] == 2
    assert retried_all.json()['failedCount'] == 0
    assert retried_all.json()['queue']['totalConcurrency'] == 1
    assert backfill.retry_download_part.await_args_list == [call(9), call(9), call(10)]
    assert cache.request.await_args_list == [
        call(9, force_remote=True),
        call(9, force_remote=True),
        call(10, force_remote=True),
    ]


def test_lists_the_paginated_analysis_queue() -> None:
    service = SimpleNamespace()
    service.analysis_queue_status = AsyncMock(
        return_value=AnalysisQueueStatus(
            active=(),
            queued=(
                AnalysisQueueItem(
                    part_id=3,
                    session_id=7,
                    part_index=1,
                    title='今天的直播',
                    anchor_name='主播',
                    state='pending',
                    stage='video_scan',
                    category='realtime',
                    progress=0,
                    requested_at=1_000,
                    started_at=None,
                    updated_at=1_001,
                    live_started_at=900,
                    part_duration_seconds=3_600,
                    recording_duration_seconds=3_600,
                    match_count=0,
                    part_count=1,
                    completed_part_count=0,
                ),
            ),
            pending_count=21,
            manual_pending=1,
            realtime_pending=2,
            archive_pending=18,
            migration_pending=0,
            backlog_pending=0,
        )
    )
    application = FastAPI()
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[vainglory.authenticated_manager_subject] = (
        lambda: 'manager'
    )
    application.dependency_overrides[vainglory.get_service] = lambda: service

    with TestClient(application) as client:
        response = client.get(
            '/api/v1/vainglory/analysis-queue-items', params={'limit': 20, 'offset': 20}
        )

    assert response.status_code == 200
    assert response.json()['total'] == 21
    assert response.json()['items'][0]['category'] == 'realtime'
    service.analysis_queue_status.assert_awaited_once_with(limit=20, offset=20)


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
        desired_concurrency=4,
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
            '/api/v1/vainglory/workers/mac-studio',
            json={'enabled': False, 'desiredConcurrency': 4},
        )
        invalid = client.post(
            '/api/v1/vainglory/workers', json={'workerId': '../not-allowed'}
        )

    assert listed.status_code == 200
    assert listed.json()['workers'][0]['activePartIds'] == [7]
    assert listed.json()['workers'][0]['profiledTaskCount'] == 10
    assert listed.json()['workers'][0]['desiredConcurrency'] == 4
    assert listed.json()['workers'][0]['profiledVideoSeconds'] == 18_000.0
    assert listed.json()['workers'][0]['totalDecodeAnalysisSeconds'] == 600.0
    assert listed.json()['workers'][0]['totalProfiledTaskSeconds'] == 900.0
    assert created.status_code == 201
    assert updated.status_code == 200
    assert updated.json()['enabled'] is False
    assert invalid.status_code == 422
    service.add_analysis_worker.assert_awaited_once_with('mac-studio', 'Mac Studio')
    service.update_analysis_worker.assert_awaited_once_with(
        'mac-studio', display_name=None, enabled=False, desired_concurrency=4
    )


def test_worker_reads_desired_concurrency() -> None:
    service = SimpleNamespace()
    service.analysis_worker_configuration = AsyncMock(return_value=4)
    application = FastAPI()
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[
        vainglory.security.authenticated_analysis_worker
    ] = lambda: 'analysis-worker'
    application.dependency_overrides[vainglory.get_service] = lambda: service

    with TestClient(application) as client:
        response = client.post(
            '/api/v1/vainglory/worker/configuration',
            json={
                'workerId': 'mac-studio',
                'modelPackageId': 'vg-vision-v2',
                'pipelineVersion': 'timeline-v2',
                'concurrency': 2,
            },
        )

    assert response.status_code == 200
    assert response.json() == {'desiredConcurrency': 4}
    service.analysis_worker_configuration.assert_awaited_once_with(
        worker_id='mac-studio',
        model_package_id='vg-vision-v2',
        pipeline_version='timeline-v2',
        concurrency=2,
    )


def test_worker_afk_claim_includes_expected_team_size() -> None:
    service = SimpleNamespace()
    service.claim_remote_work = AsyncMock(
        return_value=RemoteAnalysisClaim(
            kind='afk_status_backfill',
            item_id=7,
            frame_png=b'result-frame',
            team_size=5,
        )
    )
    application = FastAPI()
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[
        vainglory.security.authenticated_analysis_worker
    ] = lambda: 'analysis-worker'
    application.dependency_overrides[vainglory.get_service] = lambda: service

    with TestClient(application) as client:
        response = client.post(
            '/api/v1/vainglory/worker/claim',
            json={
                'workerId': 'mac-studio',
                'modelPackageId': 'vg-vision-v3',
                'pipelineVersion': 'timeline-v2',
                'concurrency': 1,
                'queue': 'image',
            },
        )

    assert response.status_code == 200
    assert response.json()['teamSize'] == 5
    service.claim_remote_work.assert_awaited_once_with(
        worker_id='mac-studio',
        model_package_id='vg-vision-v3',
        pipeline_version='timeline-v2',
        concurrency=1,
        queue='image',
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


def test_live_worker_claim_uses_frozen_recording_snapshot(monkeypatch) -> None:
    service = SimpleNamespace(
        claim_remote_live_work=AsyncMock(
            return_value=LiveAnalysisClaim(
                kind='coarse',
                item_id=7,
                session_id=9,
                part=VideoPart(id=7, index=2, path='/recording.flv'),
                lease_owner='mac-studio',
                lease_generation=3,
            )
        ),
        fail_remote_live_work=AsyncMock(),
    )
    monkeypatch.setattr(
        vainglory.recording_sessions_router, 'get_content_reader', lambda: object()
    )
    monkeypatch.setattr(
        vainglory.recording_sessions_router,
        'create_recording_media_access',
        AsyncMock(
            return_value=SimpleNamespace(
                token='signed',
                expires_at=2_000,
                snapshot_id='snapshot-one',
                duration_ms=60_000,
            )
        ),
    )
    application = FastAPI()
    application.include_router(vainglory.router, prefix='/api/v1')
    application.dependency_overrides[
        vainglory.security.authenticated_analysis_worker
    ] = lambda: 'analysis-worker'
    application.dependency_overrides[vainglory.get_service] = lambda: service

    with TestClient(application) as client:
        response = client.post(
            '/api/v1/vainglory/worker/live/claim',
            json={'workerId': 'mac-studio', 'concurrency': 3},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload['targetAtMs'] == 59_500
    assert 'media_snapshot=snapshot-one' in payload['mediaPath']
    assert 'media_token=signed' in payload['mediaPath']
