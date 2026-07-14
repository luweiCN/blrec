from pathlib import Path
from typing import Dict, Iterator, Sequence, Tuple
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.journal import (
    DanmakuItemProgress,
    RecordingPart,
    RecordingSession,
    UploadJobProgress,
    UploadPartProgress,
)
from blrec.bili_upload.recording_content import (
    DanmakuLine,
    DanmakuPage,
    MediaResource,
    RecordingContentNotFound,
    RecordingContentUnavailable,
)
from blrec.web import security
from blrec.web.routers import recording_sessions


class FakeJournal:
    degraded_reason = None

    async def count_sessions(self) -> int:
        return 41

    async def list_sessions(
        self, *, limit: int = 50, offset: int = 0
    ) -> Tuple[RecordingSession, ...]:
        assert limit == 20
        assert offset == 40
        part = RecordingPart(
            id=2,
            session_id=1,
            run_id='run-1',
            part_index=1,
            source_path='/rec/p1.flv',
            final_path='/rec/p1.mp4',
            xml_path='/rec/p1.xml',
            record_start_time=901,
            artifact_state='ready',
            xml_completed=True,
            source_exists=False,
            final_exists=True,
            error_message=None,
            record_end_time=960,
            record_duration_seconds=59,
            file_size_bytes=1_048_576,
            danmaku_count=321,
        )
        return (
            RecordingSession(
                id=1,
                room_id=100,
                broadcast_session_key='100:900',
                live_start_time=900,
                state='closed',
                started_at=900,
                ended_at=1_000,
                title='今晚挑战通关',
                cover_url='https://example.invalid/cover.jpg',
                cover_path='/rec/cover.jpg',
                anchor_uid=42,
                anchor_name='主播名',
                area_id=1,
                area_name='单机游戏',
                parent_area_id=2,
                parent_area_name='游戏',
                live_end_time=1_000,
                parts=(part,),
            ),
        )

    async def upload_jobs_for_sessions(
        self, session_ids: Sequence[int]
    ) -> Dict[int, UploadJobProgress]:
        assert tuple(session_ids) == (1,)
        return {
            1: UploadJobProgress(
                id=9,
                session_id=1,
                account_id=7,
                account_uid=42,
                account_display_name='投稿账号',
                state='waiting_review',
                submit_state='confirmed',
                comment_branch_state='pending',
                danmaku_branch_state='pending',
                aid=123,
                bvid='BV1test',
                review_reason='等待 B 站审核',
                attempt=2,
                next_attempt_at=1_100,
                created_at=1_001,
                updated_at=1_050,
                danmaku_total=1,
                danmaku_confirmed=0,
                danmaku_pending=0,
                danmaku_unknown=1,
                danmaku_failed=0,
                unknown_danmaku_items=(
                    DanmakuItemProgress(
                        id=11,
                        part_index=1,
                        progress_ms=12_000,
                        content='需要确认的弹幕',
                        error_message='远端结果未知',
                    ),
                ),
                parts=(
                    UploadPartProgress(
                        id=10,
                        job_id=9,
                        part_index=1,
                        upload_state='confirmed',
                        danmaku_import_state='pending',
                        remote_filename='remote-p1',
                        cid=None,
                    ),
                ),
            )
        }


class FakeContentReader:
    def __init__(self, media_path: Path) -> None:
        self.media_path = media_path

    async def media(self, part_id: int) -> MediaResource:
        if part_id == 404:
            raise RecordingContentNotFound('录制分 P 不存在')
        if part_id == 409:
            raise RecordingContentUnavailable('该分 P 的本地视频不可用')
        return MediaResource(
            path=str(self.media_path),
            size=10,
            content_type='video/x-flv',
            recording=True,
            part_index=1,
            bvid=None,
            remote_available=False,
        )

    async def danmaku(self, part_id: int, *, cursor: int, limit: int) -> DanmakuPage:
        assert part_id == 2
        assert cursor == 3
        assert limit == 2
        return DanmakuPage(
            items=(
                DanmakuLine(
                    index=3,
                    progress_ms=1_250,
                    mode=1,
                    font_size=25,
                    color=16_777_215,
                    content='第一条',
                ),
                DanmakuLine(
                    index=4,
                    progress_ms=2_500,
                    mode=4,
                    font_size=18,
                    color=255,
                    content='<script>不会执行</script>',
                ),
            ),
            next_cursor=5,
        )


@pytest.fixture(autouse=True)
def restore_router_state() -> Iterator[None]:
    old_journal = recording_sessions.journal
    old_reason = recording_sessions.unavailable_reason
    old_publisher = recording_sessions.danmaku_publisher
    had_content_reader = hasattr(recording_sessions, 'content_reader')
    old_content_reader = getattr(recording_sessions, 'content_reader', None)
    old_key = security.api_key
    yield
    recording_sessions.journal = old_journal
    recording_sessions.unavailable_reason = old_reason
    recording_sessions.danmaku_publisher = old_publisher
    if had_content_reader:
        recording_sessions.content_reader = old_content_reader
    elif hasattr(recording_sessions, 'content_reader'):
        delattr(recording_sessions, 'content_reader')
    security.api_key = old_key


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    api = FastAPI()
    api.include_router(recording_sessions.router, prefix='/api/v1')
    security.api_key = 'test-api-key'
    recording_sessions.journal = FakeJournal()  # type: ignore[assignment]
    recording_sessions.danmaku_publisher = AsyncMock()
    media = tmp_path / 'part.flv'
    media.write_bytes(b'0123456789')
    recording_sessions.content_reader = FakeContentReader(media)
    recording_sessions.unavailable_reason = None
    with TestClient(api) as value:
        yield value


def test_list_recording_sessions_returns_redacted_part_state(
    client: TestClient,
) -> None:
    response = client.get(
        '/api/v1/recording-sessions?limit=20&offset=40',
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 200
    assert response.json() == {
        'degradedReason': None,
        'total': 41,
        'sessions': [
            {
                'id': 1,
                'roomId': 100,
                'broadcastSessionKey': '100:900',
                'liveStartTime': 900,
                'state': 'closed',
                'startedAt': 900,
                'endedAt': 1_000,
                'title': '今晚挑战通关',
                'coverUrl': 'https://example.invalid/cover.jpg',
                'coverPath': '/rec/cover.jpg',
                'anchorUid': 42,
                'anchorName': '主播名',
                'areaId': 1,
                'areaName': '单机游戏',
                'parentAreaId': 2,
                'parentAreaName': '游戏',
                'liveEndTime': 1_000,
                'partCount': 1,
                'danmakuCount': 321,
                'totalFileSizeBytes': 1_048_576,
                'recordDurationSeconds': 59,
                'uploadJob': {
                    'id': 9,
                    'accountId': 7,
                    'accountUid': 42,
                    'accountDisplayName': '投稿账号',
                    'state': 'waiting_review',
                    'submitState': 'confirmed',
                    'commentBranchState': 'pending',
                    'danmakuBranchState': 'pending',
                    'aid': 123,
                    'bvid': 'BV1test',
                    'reviewReason': '等待 B 站审核',
                    'attempt': 2,
                    'nextAttemptAt': 1_100,
                    'createdAt': 1_001,
                    'updatedAt': 1_050,
                    'danmakuTotal': 1,
                    'danmakuConfirmed': 0,
                    'danmakuPending': 0,
                    'danmakuUnknown': 1,
                    'danmakuFailed': 0,
                    'unknownDanmakuItems': [
                        {
                            'id': 11,
                            'partIndex': 1,
                            'progressMs': 12_000,
                            'content': '需要确认的弹幕',
                            'errorMessage': '远端结果未知',
                        }
                    ],
                    'parts': [
                        {
                            'id': 10,
                            'partIndex': 1,
                            'uploadState': 'confirmed',
                            'danmakuImportState': 'pending',
                            'remoteFilename': 'remote-p1',
                            'cid': None,
                        }
                    ],
                },
                'parts': [
                    {
                        'id': 2,
                        'runId': 'run-1',
                        'partIndex': 1,
                        'sourcePath': '/rec/p1.flv',
                        'finalPath': '/rec/p1.mp4',
                        'xmlPath': '/rec/p1.xml',
                        'recordStartTime': 901,
                        'recordEndTime': 960,
                        'recordDurationSeconds': 59,
                        'fileSizeBytes': 1_048_576,
                        'danmakuCount': 321,
                        'artifactState': 'ready',
                        'xmlCompleted': True,
                        'sourceExists': False,
                        'finalExists': True,
                        'errorMessage': None,
                    }
                ],
            }
        ],
    }
    assert 'cookie' not in response.text.lower()
    assert 'token' not in response.text.lower()


def test_unavailable_journal_returns_503(client: TestClient) -> None:
    recording_sessions.journal = None
    recording_sessions.unavailable_reason = 'upload database is unavailable'

    response = client.get(
        '/api/v1/recording-sessions', headers={'x-api-key': 'test-api-key'}
    )

    assert response.status_code == 503
    assert response.json()['detail'] == 'upload database is unavailable'


def test_unknown_danmaku_decision_requires_auth_and_reason(client: TestClient) -> None:
    publisher = recording_sessions.danmaku_publisher
    assert isinstance(publisher, AsyncMock)

    response = client.post(
        '/api/v1/recording-sessions/danmaku-items/11/decision',
        headers={'x-api-key': 'test-api-key'},
        json={'action': 'assume_success', 'reason': '已在稿件页面确认存在'},
    )

    assert response.status_code == 204
    publisher.assume_success.assert_awaited_once()
    assert (
        publisher.assume_success.await_args.kwargs['reason'] == '已在稿件页面确认存在'
    )
    assert publisher.assume_success.await_args.kwargs['manager_subject']

    invalid = client.post(
        '/api/v1/recording-sessions/danmaku-items/11/decision',
        headers={'x-api-key': 'test-api-key'},
        json={'action': 'retry_accept_duplicate_risk', 'reason': ''},
    )
    assert invalid.status_code == 422


def test_media_returns_the_fixed_file_snapshot(client: TestClient) -> None:
    response = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 200
    assert response.content == b'0123456789'
    assert response.headers['accept-ranges'] == 'bytes'
    assert response.headers['content-length'] == '10'
    assert response.headers['content-type'] == 'video/x-flv'


def test_media_range_returns_the_requested_slice(client: TestClient) -> None:
    response = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        headers={'x-api-key': 'test-api-key', 'range': 'bytes=2-4'},
    )

    assert response.status_code == 206
    assert response.headers['content-range'] == 'bytes 2-4/10'
    assert response.headers['content-length'] == '3'
    assert response.content == b'234'


@pytest.mark.parametrize(
    'value', ('bytes=20-30', 'bytes=4-2', 'bytes=0-1,4-5', 'items=0-1')
)
def test_media_rejects_invalid_or_unsatisfiable_ranges(
    client: TestClient, value: str
) -> None:
    response = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        headers={'x-api-key': 'test-api-key', 'range': value},
    )

    assert response.status_code == 416
    assert response.headers['content-range'] == 'bytes */10'


def test_media_maps_missing_and_unavailable_parts(client: TestClient) -> None:
    missing = client.get(
        '/api/v1/recording-sessions/parts/404/media',
        headers={'x-api-key': 'test-api-key'},
    )
    unavailable = client.get(
        '/api/v1/recording-sessions/parts/409/media',
        headers={'x-api-key': 'test-api-key'},
    )

    assert missing.status_code == 404
    assert unavailable.status_code == 409


def test_media_requires_authentication(client: TestClient) -> None:
    response = client.get('/api/v1/recording-sessions/parts/2/media')

    assert response.status_code == 401


def test_media_access_token_authorizes_range_requests_without_exposing_api_key(
    client: TestClient,
) -> None:
    access = client.post(
        '/api/v1/recording-sessions/parts/2/media-access',
        headers={'x-api-key': 'test-api-key'},
    )

    assert access.status_code == 200
    assert access.json()['token']
    assert access.json()['expiresAt'] > 0
    assert 'test-api-key' not in access.text

    response = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        params={
            'media_token': access.json()['token'],
            'media_expires': access.json()['expiresAt'],
        },
        headers={'range': 'bytes=5-'},
    )
    assert response.status_code == 206
    assert response.content == b'56789'


def test_media_access_rejects_a_tampered_token(client: TestClient) -> None:
    access = client.post(
        '/api/v1/recording-sessions/parts/2/media-access',
        headers={'x-api-key': 'test-api-key'},
    ).json()

    response = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        params={
            'media_token': access['token'] + 'tampered',
            'media_expires': access['expiresAt'],
        },
    )

    assert response.status_code == 401


def test_danmaku_returns_a_camel_case_page(client: TestClient) -> None:
    response = client.get(
        '/api/v1/recording-sessions/parts/2/danmaku?cursor=3&limit=2',
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 200
    assert response.json() == {
        'items': [
            {
                'index': 3,
                'progressMs': 1_250,
                'mode': 1,
                'fontSize': 25,
                'color': 16_777_215,
                'content': '第一条',
            },
            {
                'index': 4,
                'progressMs': 2_500,
                'mode': 4,
                'fontSize': 18,
                'color': 255,
                'content': '<script>不会执行</script>',
            },
        ],
        'nextCursor': 5,
    }


def test_danmaku_rejects_pages_over_one_hundred(client: TestClient) -> None:
    response = client.get(
        '/api/v1/recording-sessions/parts/2/danmaku?cursor=0&limit=101',
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 422
