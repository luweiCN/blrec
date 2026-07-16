from typing import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.highlight_cut import (
    ClipInspection,
    InspectedClipSource,
    MediaProfile,
)
from blrec.bili_upload.highlights import (
    HighlightClip,
    HighlightClipSource,
    HighlightMarker,
    HighlightRangeUnavailable,
    HighlightTimeline,
    MappedHighlight,
    TimelinePart,
)
from blrec.web import security
from blrec.web.routers import highlights


def marker() -> HighlightMarker:
    return HighlightMarker(
        id=1,
        room_id=100,
        observed_at_ms=1_100_000,
        player_delay_ms=20_000,
        content_at_ms=1_080_000,
        title='测试直播',
        anchor_name='主播',
        name='测试直播 高光 12:00:00',
        note='',
        source='web',
        created_at=1_100,
        updated_at=1_100,
    )


def inspection() -> ClipInspection:
    profile = MediaProfile('h264', 1920, 1080, '60/1', 42, 120_000, True)
    return ClipInspection(
        sources=(InspectedClipSource(1, '/rec/p1.flv', 18_000, 70_000, 0, profile),),
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        actual_start_ms=18_000,
        actual_end_ms=70_000,
        extra_lead_ms=2_000,
        confirmation_required=False,
    )


def clip() -> HighlightClip:
    return HighlightClip(
        id=3,
        marker_id=1,
        room_id=100,
        source_session_id=9,
        upload_session_id=None,
        name='第一段高光',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        actual_start_ms=18_000,
        actual_end_ms=70_000,
        output_video_path='/rec/highlights/100/highlight-3.mp4',
        output_xml_path='/rec/highlights/100/highlight-3.xml',
        state='queued',
        confirmation_required=False,
        confirmed=False,
        error_message=None,
        attempt=0,
        created_at=1_100,
        updated_at=1_100,
        sources=(HighlightClipSource(1, 1, 20_000, 70_000, 18_000, 70_000),),
    )


class FakeHighlightService:
    def __init__(self) -> None:
        self.create_marker = AsyncMock(return_value=marker())
        self.update_marker = AsyncMock(return_value=marker())
        self.delete_marker = AsyncMock(return_value=None)
        self.inspect_clip = AsyncMock(return_value=inspection())
        self.create_clip = AsyncMock(return_value=clip())
        self.get_clip = AsyncMock(return_value=clip())
        self.delete_clip = AsyncMock(return_value='cancelled')

    async def timeline(self, session_id: int, active_durations_ms):
        value = marker()
        return HighlightTimeline(
            session_id=session_id,
            room_id=100,
            duration_ms=120_000,
            stable_end_ms=110_000,
            parts=(
                TimelinePart(
                    part_id=1,
                    part_index=1,
                    path='/rec/p1.flv',
                    absolute_start_at_ms=1_000_000,
                    timeline_start_ms=0,
                    duration_ms=120_000,
                    stable_end_ms=110_000,
                    recording=True,
                ),
            ),
            markers=(MappedHighlight(value, 1, 80_000, 80_000),),
        )


@pytest.fixture(autouse=True)
def restore_router_state() -> Iterator[None]:
    old_service = highlights.service
    old_worker = highlights.worker
    old_creator = highlights.upload_task_creator
    old_durations = highlights.active_durations_provider
    old_key = security.api_key
    yield
    highlights.service = old_service
    highlights.worker = old_worker
    highlights.upload_task_creator = old_creator
    highlights.active_durations_provider = old_durations
    security.api_key = old_key


@pytest.fixture
def client() -> Iterator[TestClient]:
    api = FastAPI(dependencies=[Depends(security.authenticate)])
    api.include_router(highlights.router, prefix='/api/v1')
    security.api_key = 'test-api-key'
    highlights.service = FakeHighlightService()  # type: ignore[assignment]
    highlights.worker = AsyncMock()
    highlights.upload_task_creator = AsyncMock(return_value=17)
    highlights.active_durations_provider = AsyncMock(return_value={1: 120_000})
    with TestClient(api) as value:
        yield value


def auth() -> dict:
    return {'x-api-key': 'test-api-key'}


def test_marker_crud_is_authenticated_and_uses_camel_case(client: TestClient) -> None:
    unauthorized = client.post(
        '/api/v1/highlights', json={'roomId': 100, 'observedAtMs': 1_100_000}
    )
    assert unauthorized.status_code == 401

    created = client.post(
        '/api/v1/highlights',
        headers=auth(),
        json={
            'roomId': 100,
            'observedAtMs': 1_100_000,
            'playerDelayMs': 20_000,
            'title': '测试直播',
            'anchorName': '主播',
            'source': 'web',
        },
    )
    assert created.status_code == 201
    assert created.json()['contentAtMs'] == 1_080_000

    updated = client.patch(
        '/api/v1/highlights/1',
        headers=auth(),
        json={'name': '重命名', 'note': '剪这里'},
    )
    assert updated.status_code == 200
    deleted = client.delete('/api/v1/highlights/1', headers=auth())
    assert deleted.status_code == 204


def test_timeline_inspection_and_clip_lifecycle(client: TestClient) -> None:
    timeline = client.get('/api/v1/highlights/sessions/9/timeline', headers=auth())
    assert timeline.status_code == 200
    assert timeline.json()['parts'][0]['stableEndMs'] == 110_000
    assert timeline.json()['parts'][0]['mediaKind'] == 'flv'
    assert timeline.json()['markers'][0]['timelineOffsetMs'] == 80_000

    inspected = client.post(
        '/api/v1/highlights/sessions/9/clips/inspect',
        headers=auth(),
        json={'startMs': 20_000, 'endMs': 70_000},
    )
    assert inspected.status_code == 200
    assert inspected.json()['actualStartMs'] == 18_000
    assert inspected.json()['confirmationRequired'] is False

    created = client.post(
        '/api/v1/highlights/sessions/9/clips',
        headers=auth(),
        json={
            'markerId': 1,
            'name': '第一段高光',
            'startMs': 20_000,
            'endMs': 70_000,
            'confirmKeyframe': False,
        },
    )
    assert created.status_code == 201
    assert created.json()['state'] == 'queued'
    fetched = client.get('/api/v1/highlights/clips/3', headers=auth())
    assert fetched.status_code == 200

    upload = client.post('/api/v1/highlights/clips/3/upload-task', headers=auth())
    assert upload.status_code == 201
    assert upload.json() == {'jobId': 17}
    deleted = client.delete('/api/v1/highlights/clips/3', headers=auth())
    assert deleted.status_code == 204


def test_unsafe_clip_range_returns_conflict(client: TestClient) -> None:
    service = highlights.service
    assert isinstance(service, FakeHighlightService)
    service.create_clip.side_effect = HighlightRangeUnavailable(
        '所选范围进入录制中的最后 10 秒'
    )

    response = client.post(
        '/api/v1/highlights/sessions/9/clips',
        headers=auth(),
        json={'name': '过近', 'startMs': 100_000, 'endMs': 119_000},
    )

    assert response.status_code == 409
    assert '最后 10 秒' in response.json()['detail']
