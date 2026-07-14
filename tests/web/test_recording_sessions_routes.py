from typing import Iterator, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.journal import RecordingPart, RecordingSession
from blrec.web import security
from blrec.web.routers import recording_sessions


class FakeJournal:
    degraded_reason = None

    async def list_sessions(self, *, limit: int = 50) -> Tuple[RecordingSession, ...]:
        assert limit == 20
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


@pytest.fixture(autouse=True)
def restore_router_state() -> Iterator[None]:
    old_journal = recording_sessions.journal
    old_reason = recording_sessions.unavailable_reason
    old_key = security.api_key
    yield
    recording_sessions.journal = old_journal
    recording_sessions.unavailable_reason = old_reason
    security.api_key = old_key


@pytest.fixture
def client() -> Iterator[TestClient]:
    api = FastAPI()
    api.include_router(recording_sessions.router, prefix='/api/v1')
    security.api_key = 'test-api-key'
    recording_sessions.journal = FakeJournal()  # type: ignore[assignment]
    recording_sessions.unavailable_reason = None
    with TestClient(api) as value:
        yield value


def test_list_recording_sessions_returns_redacted_part_state(
    client: TestClient,
) -> None:
    response = client.get(
        '/api/v1/recording-sessions?limit=20', headers={'x-api-key': 'test-api-key'}
    )

    assert response.status_code == 200
    assert response.json() == {
        'degradedReason': None,
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
