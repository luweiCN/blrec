from dataclasses import dataclass
from pathlib import Path
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


@dataclass(frozen=True)
class MarkerCount:
    part_id: int
    count: int


class FakeHighlightService:
    def __init__(self) -> None:
        self.create_marker = AsyncMock(return_value=marker())
        self.update_marker = AsyncMock(return_value=marker())
        self.delete_marker = AsyncMock(return_value=None)
        self.inspect_clip = AsyncMock(return_value=inspection())
        self.create_clip = AsyncMock(return_value=clip())
        self.list_clips = AsyncMock(return_value=(clip(),))
        self.list_all_clips = AsyncMock(return_value=(1, (clip(),)))
        self.get_clip = AsyncMock(return_value=clip())
        self.retry_clip = AsyncMock(return_value=clip())
        self.delete_clip = AsyncMock(return_value='cancelled')
        self.clip_video_path = AsyncMock()
        self.ensure_upload_session = AsyncMock(return_value=12)
        self.marker_counts = AsyncMock(
            return_value=(MarkerCount(1, 2), MarkerCount(2, 0))
        )

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
    old_deleter = highlights.clip_deleter
    old_durations = highlights.active_durations_provider
    old_key = security.api_key
    yield
    highlights.service = old_service
    highlights.worker = old_worker
    highlights.upload_task_creator = old_creator
    highlights.clip_deleter = old_deleter
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
    highlights.clip_deleter = AsyncMock(return_value='deleted')
    highlights.active_durations_provider = AsyncMock(return_value={1: 120_000})
    with TestClient(api) as value:
        yield value


def auth() -> dict:
    return {'x-api-key': 'test-api-key'}


def test_global_clip_library_route_is_paginated(client: TestClient) -> None:
    response = client.get('/api/v1/highlights/clips?limit=20&offset=0', headers=auth())

    assert response.status_code == 200
    assert response.json()['total'] == 1
    assert response.json()['items'][0]['name'] == '第一段高光'
    service = highlights.service
    assert service is not None
    service.list_all_clips.assert_awaited_once_with(limit=20, offset=0)


def upload_settings() -> dict:
    return {
        'accountMode': 'primary',
        'accountId': None,
        'enabled': True,
        'titleTemplate': '{{ title }} 精选',
        'descriptionTemplate': '高光片段',
        'partTitleTemplate': 'P{{ part_index }}',
        'dynamicTemplate': '高光片段',
        'tid': 21,
        'tags': '高光,直播',
        'creationStatementId': -1,
        'originalAuthorization': False,
        'source': '',
        'isOnlySelf': False,
        'publishDynamic': True,
        'upSelectionReply': False,
        'upCloseReply': False,
        'upCloseDanmu': False,
        'autoComment': True,
        'danmakuBackfill': True,
        'filters': {},
        'collectionSeasonId': 20,
        'collectionSectionId': 21,
        'coverMode': 'live',
        'coverAssetId': None,
        'publishDelaySeconds': 0,
        'retentionMode': 'submitted',
        'retentionDays': 5,
    }


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


def test_marker_counts_are_authenticated_and_do_not_load_the_timeline(
    client: TestClient,
) -> None:
    unauthorized = client.get('/api/v1/highlights/sessions/9/marker-counts')
    assert unauthorized.status_code == 401

    response = client.get('/api/v1/highlights/sessions/9/marker-counts', headers=auth())

    assert response.status_code == 200
    assert response.json() == [{'partId': 1, 'count': 2}, {'partId': 2, 'count': 0}]
    service = highlights.service
    assert isinstance(service, FakeHighlightService)
    service.marker_counts.assert_awaited_once_with(9)
    durations = highlights.active_durations_provider
    assert isinstance(durations, AsyncMock)
    durations.assert_not_awaited()


def test_marker_counts_return_not_found_for_an_unknown_session(
    client: TestClient,
) -> None:
    service = highlights.service
    assert isinstance(service, FakeHighlightService)
    service.marker_counts.side_effect = ValueError("unknown live recording session '9'")

    response = client.get('/api/v1/highlights/sessions/9/marker-counts', headers=auth())

    assert response.status_code == 404
    assert 'unknown live recording session' in response.json()['detail']


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

    retried = client.post('/api/v1/highlights/clips/3/retry', headers=auth())
    assert retried.status_code == 200
    service = highlights.service
    assert isinstance(service, FakeHighlightService)
    service.retry_clip.assert_awaited_once_with(3)

    listed = client.get('/api/v1/highlights/sessions/9/clips', headers=auth())
    assert listed.status_code == 200
    assert listed.json()[0]['name'] == '第一段高光'

    prepared = client.post('/api/v1/highlights/clips/3/upload-session', headers=auth())
    assert prepared.status_code == 201
    assert prepared.json() == {'sessionId': 12}

    upload = client.post(
        '/api/v1/highlights/clips/3/upload-task', headers=auth(), json=upload_settings()
    )
    assert upload.status_code == 201
    assert upload.json() == {'jobId': 17}
    creator = highlights.upload_task_creator
    assert isinstance(creator, AsyncMock)
    assert creator.await_args.kwargs['settings'].collection_section_id == 21
    deleted = client.delete('/api/v1/highlights/clips/3', headers=auth())
    assert deleted.status_code == 204
    deleter = highlights.clip_deleter
    assert isinstance(deleter, AsyncMock)
    deleter.assert_awaited_once_with(3)


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


def test_ready_clip_supports_signed_byte_range_playback(
    client: TestClient, tmp_path: Path
) -> None:
    video = tmp_path / 'highlight-3.mp4'
    video.write_bytes(b'0123456789')
    service = highlights.service
    assert isinstance(service, FakeHighlightService)
    service.clip_video_path.return_value = video

    access = client.post('/api/v1/highlights/clips/3/media-access', headers=auth())

    assert access.status_code == 200
    payload = access.json()
    media = client.get(
        '/api/v1/highlights/clips/3/media',
        params={'media_token': payload['token'], 'media_expires': payload['expiresAt']},
        headers={'Range': 'bytes=2-5'},
    )
    assert media.status_code == 206
    assert media.content == b'2345'
    assert media.headers['content-range'] == 'bytes 2-5/10'
    assert media.headers['accept-ranges'] == 'bytes'

    download = client.get(
        '/api/v1/highlights/clips/3/media',
        params={
            'media_token': payload['token'],
            'media_expires': payload['expiresAt'],
            'download': 1,
        },
    )
    assert download.status_code == 200
    assert download.headers['content-disposition'] == (
        "attachment; filename*=UTF-8''"
        '%E7%AC%AC%E4%B8%80%E6%AE%B5%E9%AB%98%E5%85%89.mp4'
    )
