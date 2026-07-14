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


@pytest.fixture(autouse=True)
def restore_router_state() -> Iterator[None]:
    old_journal = recording_sessions.journal
    old_reason = recording_sessions.unavailable_reason
    old_publisher = recording_sessions.danmaku_publisher
    old_key = security.api_key
    yield
    recording_sessions.journal = old_journal
    recording_sessions.unavailable_reason = old_reason
    recording_sessions.danmaku_publisher = old_publisher
    security.api_key = old_key


@pytest.fixture
def client() -> Iterator[TestClient]:
    api = FastAPI()
    api.include_router(recording_sessions.router, prefix='/api/v1')
    security.api_key = 'test-api-key'
    recording_sessions.journal = FakeJournal()  # type: ignore[assignment]
    recording_sessions.danmaku_publisher = AsyncMock()
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
