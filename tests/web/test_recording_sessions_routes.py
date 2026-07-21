from dataclasses import asdict, replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Sequence, Tuple
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.active_media import ActiveMediaBusy, ActiveMediaMetadata
from blrec.bili_upload.journal import (
    DanmakuItemProgress,
    RecordingPart,
    RecordingSession,
    RecordingSessionSummary,
    UploadJobProgress,
    UploadJobSummary,
    UploadPartProgress,
)
from blrec.bili_upload.policies import default_room_upload_policy
from blrec.bili_upload.recording_content import (
    DanmakuLine,
    DanmakuPage,
    FlvMediaSnapshot,
    MediaResource,
    RecordingContentCursorStale,
    RecordingContentNotFound,
    RecordingContentUnavailable,
    RecordingMediaCandidate,
    RecordingMediaDescriptor,
)
from blrec.bili_upload.session_submission import SessionSubmissionView
from blrec.bili_upload.task_actions import UploadTaskActionRejected
from blrec.flv.common import create_metadata_tag, parse_metadata
from blrec.flv.io import FlvReader, FlvWriter
from blrec.flv.models import FlvHeader
from blrec.web import security
from blrec.web.routers import recording_sessions


class FakeJournal:
    degraded_reason = None

    def __init__(self) -> None:
        self.list_filters = {}
        self.count_filters = {}
        self.upload_job_state = 'waiting_review'
        self.danmaku_branch_state = 'pending'
        self.upload_part_cid = None
        self.count_calls = 0
        self.summary_calls = 0
        self.detail_calls = 0
        self.upload_job_calls = 0

    async def count_sessions(self, **filters: object) -> int:
        self.count_calls += 1
        self.count_filters = filters
        return 41

    async def list_sessions(
        self, *, limit: int = 50, offset: int = 0, **filters: object
    ) -> Tuple[RecordingSession, ...]:
        assert limit == 20
        assert offset == 40
        self.list_filters = filters
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

    async def list_session_summaries(
        self, *, limit: int = 50, offset: int = 0, **filters: object
    ) -> Tuple[RecordingSessionSummary, ...]:
        self.summary_calls += 1
        sessions = await self.list_sessions(limit=limit, offset=offset, **filters)
        session = sessions[0]
        upload_job = self._upload_job()
        session_values = asdict(session)
        for field in ('broadcast_session_key', 'cover_path', 'parts'):
            session_values.pop(field)
        session_values.update(
            part_count=session.part_count,
            danmaku_count=session.danmaku_count,
            total_file_size_bytes=session.total_file_size_bytes,
            record_duration_seconds=session.record_duration_seconds,
            upload_job=self._upload_job_summary(upload_job),
        )
        return (RecordingSessionSummary(**session_values),)

    async def get_session(self, session_id: int) -> RecordingSession:
        if session_id != 1:
            raise ValueError("unknown recording session '{}'".format(session_id))
        self.detail_calls += 1
        return (await self.list_sessions(limit=20, offset=40))[0]

    def _upload_job(self) -> UploadJobProgress:
        return UploadJobProgress(
            id=9,
            session_id=1,
            account_id=7,
            account_uid=42,
            account_display_name='投稿账号',
            state=self.upload_job_state,
            submit_state='confirmed',
            comment_branch_state='pending',
            danmaku_branch_state=self.danmaku_branch_state,
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
            can_repair=False,
            submission_verification_state='partial',
            submission_verified_at=1_040,
            submission_verification={
                'state': 'partial',
                'checked': ['title'],
                'missing': ['up_selection_reply'],
                'mismatches': [],
            },
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
                    cid=self.upload_part_cid,
                ),
            ),
        )

    @staticmethod
    def _upload_job_summary(job: UploadJobProgress) -> UploadJobSummary:
        job_values = asdict(job)
        for field in ('parts', 'unknown_danmaku_items', 'submission_verification'):
            job_values.pop(field)
        job_values.update(
            can_backfill_danmaku=(
                job.state in ('approved', 'completed')
                and job.danmaku_branch_state == 'disabled'
                and bool(job.parts)
                and all(part.cid is not None for part in job.parts)
            ),
            confirmed_part_count=sum(
                part.upload_state == 'confirmed' for part in job.parts
            ),
            discovered_part_count=len(job.parts),
        )
        return UploadJobSummary(**job_values)

    async def upload_jobs_for_sessions(
        self, session_ids: Sequence[int]
    ) -> Dict[int, UploadJobProgress]:
        assert tuple(session_ids) == (1,)
        self.upload_job_calls += 1
        return {1: self._upload_job()}


class FakeContentReader:
    def __init__(self, media_path: Path, *, recording: bool = True) -> None:
        self.media_path = media_path
        self.recording = recording

    async def media_descriptor(self, part_id: int) -> RecordingMediaDescriptor:
        if part_id == 404:
            raise RecordingContentNotFound('录制分 P 不存在')
        if part_id == 409:
            raise RecordingContentUnavailable('该分 P 的本地视频不可用')
        return RecordingMediaDescriptor(
            part_id=part_id,
            room_id=100,
            part_index=1,
            candidates=(
                RecordingMediaCandidate(
                    path=str(self.media_path),
                    content_type='video/x-flv',
                    recording=self.recording,
                    artifact_key='recording-part:{}:source'.format(part_id),
                ),
            ),
            bvid=None,
            remote_available=False,
            index_state='pending',
        )

    async def media(self, part_id: int) -> MediaResource:
        if part_id == 404:
            raise RecordingContentNotFound('录制分 P 不存在')
        if part_id == 409:
            raise RecordingContentUnavailable('该分 P 的本地视频不可用')
        media_stat = self.media_path.stat()
        return MediaResource(
            path=str(self.media_path),
            size=10,
            content_type='video/x-flv',
            recording=True,
            room_id=100,
            part_index=1,
            bvid=None,
            remote_available=False,
            playback_mode='active_snapshot',
            index_state='pending',
            source_device=media_stat.st_dev,
            source_inode=media_stat.st_ino,
        )

    async def danmaku(self, part_id: int, *, cursor: int, limit: int) -> DanmakuPage:
        if part_id == 409:
            raise RecordingContentCursorStale('private path and cursor 100000')
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
                    user='主播',
                    uid=42,
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


class FakeActiveMediaService:
    def __init__(self) -> None:
        self.calls = []
        self.error = None

    async def snapshot(self, part_id, path, source_size, metadata):
        self.calls.append((part_id, path, source_size, metadata))
        if self.error is not None:
            raise self.error
        if isinstance(metadata, ActiveMediaMetadata):
            metadata = metadata.value
        return FlvMediaSnapshot.create(path, source_size, metadata)


class FakeSubmissionManager:
    def __init__(self) -> None:
        self.saved = None
        self.cleared = None
        self.decision = None
        self.view = SessionSubmissionView(
            session_id=1,
            room_id=100,
            decision='follow_room',
            inherited=True,
            settings_source='room',
            settings=replace(
                default_room_upload_policy(), title_template='{{ title }} 录播'
            ),
            resolution_state='pending',
            resolution_error=None,
        )

    async def get(self, session_id: int) -> SessionSubmissionView:
        assert session_id == 1
        return self.view

    async def save_override(self, session_id, command, *, manager_subject):
        self.saved = (session_id, command, manager_subject)
        self.view = replace(
            self.view, inherited=False, settings_source='session', settings=command
        )
        return self.view

    async def clear_override(self, session_id, *, manager_subject):
        self.cleared = (session_id, manager_subject)
        self.view = replace(self.view, inherited=True, settings_source='room')
        return self.view

    async def set_decision(self, session_id, decision, *, manager_subject):
        self.decision = (session_id, decision, manager_subject)
        self.view = replace(self.view, decision=decision)
        return self.view


@pytest.fixture(autouse=True)
def restore_router_state() -> Iterator[None]:
    old_journal = recording_sessions.journal
    old_reason = recording_sessions.unavailable_reason
    old_task_actions = getattr(recording_sessions, 'task_actions', None)
    old_session_action_runner = getattr(
        recording_sessions, 'session_action_runner', None
    )
    old_session_batch_runner = getattr(recording_sessions, 'session_batch_runner', None)
    old_submission_manager = getattr(recording_sessions, 'submission_manager', None)
    old_metadata_provider = getattr(
        recording_sessions, 'active_recording_metadata_provider', None
    )
    old_active_media_service = getattr(recording_sessions, 'active_media_service', None)
    had_content_reader = hasattr(recording_sessions, 'content_reader')
    old_content_reader = getattr(recording_sessions, 'content_reader', None)
    old_key = security.api_key
    yield
    recording_sessions.journal = old_journal
    recording_sessions.unavailable_reason = old_reason
    recording_sessions.task_actions = old_task_actions
    recording_sessions.session_action_runner = old_session_action_runner
    recording_sessions.session_batch_runner = old_session_batch_runner
    recording_sessions.submission_manager = old_submission_manager
    recording_sessions.active_recording_metadata_provider = old_metadata_provider
    recording_sessions.active_media_service = old_active_media_service
    if hasattr(recording_sessions, 'media_snapshot_store'):
        recording_sessions.media_snapshot_store.clear()
    if had_content_reader:
        recording_sessions.content_reader = old_content_reader
    elif hasattr(recording_sessions, 'content_reader'):
        delattr(recording_sessions, 'content_reader')
    security.api_key = old_key


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    api = FastAPI(dependencies=[Depends(security.authenticate)])
    api.include_router(recording_sessions.router, prefix='/api/v1')
    security.api_key = 'test-api-key'
    recording_sessions.journal = FakeJournal()  # type: ignore[assignment]
    recording_sessions.task_actions = AsyncMock()
    recording_sessions.session_action_runner = AsyncMock()
    recording_sessions.session_batch_runner = AsyncMock()
    recording_sessions.submission_manager = FakeSubmissionManager()
    media = tmp_path / 'part.flv'
    media.write_bytes(b'0123456789')
    recording_sessions.content_reader = FakeContentReader(media)
    recording_sessions.active_media_service = FakeActiveMediaService()
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
                'liveStartTime': 900,
                'state': 'closed',
                'startedAt': 900,
                'endedAt': 1_000,
                'title': '今晚挑战通关',
                'coverUrl': 'https://example.invalid/cover.jpg',
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
                'uploadIntent': 'none',
                'uploadDecision': 'follow_room',
                'submissionInherited': True,
                'uploadResolutionState': 'pending',
                'uploadResolutionError': None,
                'uploadSuppressed': False,
                'deletionState': 'none',
                'deletionError': None,
                'sourceKind': 'live',
                'highlightClipId': None,
                'displayState': 'waiting_review',
                'availableActions': ['delete_local'],
                'uploadJob': {
                    'id': 9,
                    'accountId': 7,
                    'accountUid': 42,
                    'accountDisplayName': '投稿账号',
                    'state': 'waiting_review',
                    'submitState': 'confirmed',
                    'preuploadFinalized': True,
                    'displayState': 'standard',
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
                    'repairState': 'idle',
                    'repairMessage': None,
                    'repairError': None,
                    'canRetry': False,
                    'canRepair': False,
                    'canSkip': False,
                    'canRepost': False,
                    'canDelete': False,
                    'operatorPaused': False,
                    'scheduledPublishAt': None,
                    'collectionBranchState': 'disabled',
                    'collectionError': None,
                    'submissionVerificationState': 'partial',
                    'submissionVerifiedAt': 1_040,
                    'commentError': None,
                    'danmakuError': None,
                    'canPause': False,
                    'canResume': False,
                    'canEdit': False,
                    'confirmedBytes': 0,
                    'totalBytes': 0,
                    'percent': 0.0,
                    'bytesPerSecond': None,
                    'etaSeconds': None,
                    'currentPartIndex': None,
                    'confirmedPartCount': 1,
                    'discoveredPartCount': 1,
                },
            }
        ],
    }
    assert 'cookie' not in response.text.lower()
    assert 'token' not in response.text.lower()


def test_recording_session_list_is_summary_and_detail_stays_complete(
    client: TestClient,
) -> None:
    headers = {'x-api-key': 'test-api-key'}

    response = client.get(
        '/api/v1/recording-sessions?limit=20&offset=40', headers=headers
    )

    assert response.status_code == 200
    item = response.json()['sessions'][0]
    assert {
        'broadcastSessionKey',
        'coverPath',
        'parts',
        'unknownDanmakuItems',
        'submissionVerification',
    }.isdisjoint(item)
    assert item['uploadJob']['accountDisplayName'] == '投稿账号'
    assert item['uploadJob']['discoveredPartCount'] == 1
    assert 'parts' not in item['uploadJob']
    assert 'unknownDanmakuItems' not in item['uploadJob']
    assert 'submissionVerification' not in item['uploadJob']
    assert 'unknownDanmakuItems' not in str(item)
    fake = recording_sessions.journal
    assert isinstance(fake, FakeJournal)
    assert fake.count_calls == 1
    assert fake.summary_calls == 1
    assert fake.upload_job_calls == 0

    detail = client.get('/api/v1/recording-sessions/1', headers=headers)

    assert detail.status_code == 200
    assert detail.json()['broadcastSessionKey'] == '100:900'
    assert detail.json()['coverPath'] == '/rec/cover.jpg'
    assert detail.json()['parts'][0]['sourcePath'] == '/rec/p1.flv'
    assert detail.json()['uploadJob']['parts'][0]['remoteFilename'] == 'remote-p1'
    assert detail.json()['uploadJob']['unknownDanmakuItems'][0]['id'] == 11
    assert detail.json()['uploadJob']['submissionVerification']['state'] == 'partial'
    assert fake.detail_calls == 1
    assert fake.upload_job_calls == 1


def test_missing_recording_session_detail_returns_not_found(client: TestClient) -> None:
    with TestClient(client.app, raise_server_exceptions=False) as non_raising_client:
        response = non_raising_client.get(
            '/api/v1/recording-sessions/404', headers={'x-api-key': 'test-api-key'}
        )

    assert response.status_code == 404
    assert response.json() == {'detail': "unknown recording session '404'"}


def test_highlight_upload_uses_the_final_submission_title() -> None:
    session = RecordingSession(
        id=2,
        room_id=100,
        broadcast_session_key='highlight:7',
        live_start_time=900,
        state='closed',
        started_at=900,
        ended_at=1_000,
        title='本地片段名称',
        source_kind='highlight',
    )
    job = UploadJobProgress(
        id=9,
        session_id=2,
        account_id=7,
        account_uid=42,
        account_display_name='投稿账号',
        state='ready',
        submit_state='prepared',
        comment_branch_state='disabled',
        danmaku_branch_state='disabled',
        aid=None,
        bvid=None,
        review_reason=None,
        attempt=0,
        next_attempt_at=0,
        created_at=1_000,
        updated_at=1_000,
        parts=(),
        title='最终投稿标题',
    )

    response = recording_sessions._session_response(session, job)

    assert response.title == '最终投稿标题'


@pytest.mark.asyncio
async def test_preupload_session_keeps_submission_settings_editable() -> None:
    journal = FakeJournal()
    session = (await journal.list_sessions(limit=20, offset=40))[0]
    upload_job = (await journal.upload_jobs_for_sessions((session.id,)))[session.id]

    _display_state, actions = recording_sessions._session_display(
        replace(session, state='open', live_end_time=None, ended_at=None),
        replace(
            upload_job,
            state='waiting_artifacts',
            submit_state='prepared',
            preupload_finalized=False,
            display_state='preuploaded_waiting',
        ),
    )

    assert 'edit_submission' in actions


def test_list_recording_sessions_passes_server_side_filters(client: TestClient) -> None:
    response = client.get(
        '/api/v1/recording-sessions',
        params={
            'limit': 20,
            'offset': 40,
            'q': '主播',
            'recordingState': 'closed',
            'uploadState': 'approved',
            'startedFrom': 100,
            'startedTo': 200,
            'sort': 'oldest',
        },
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 200
    fake = recording_sessions.journal
    assert isinstance(fake, FakeJournal)
    expected = {
        'scope': 'recordings',
        'query': '主播',
        'session_state': 'closed',
        'upload_state': 'approved',
        'started_from': 100,
        'started_to': 200,
    }
    assert fake.count_filters == expected
    assert fake.list_filters == {**expected, 'sort_order': 'oldest'}


def test_upload_scope_is_forwarded_to_server_side_query(client: TestClient) -> None:
    response = client.get(
        '/api/v1/recording-sessions',
        params={'limit': 20, 'offset': 40, 'scope': 'uploads'},
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 200
    fake = recording_sessions.journal
    assert isinstance(fake, FakeJournal)
    assert fake.count_filters['scope'] == 'uploads'
    assert fake.list_filters['scope'] == 'uploads'


def test_recording_submission_settings_support_override_restore_and_decision(
    client: TestClient,
) -> None:
    headers = {'x-api-key': 'test-api-key'}
    response = client.get(
        '/api/v1/recording-sessions/1/submission-settings', headers=headers
    )
    assert response.status_code == 200
    assert response.json()['inherited'] is True
    assert response.json()['settings']['titleTemplate'] == '{{ title }} 录播'

    payload = asdict(default_room_upload_policy())
    payload['title_template'] = '本场 {{ title }}'
    response = client.put(
        '/api/v1/recording-sessions/1/submission-settings',
        headers=headers,
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()['inherited'] is False
    assert response.json()['settings']['titleTemplate'] == '本场 {{ title }}'

    response = client.patch(
        '/api/v1/recording-sessions/1/submission-decision',
        headers=headers,
        json={'decision': 'skip'},
    )
    assert response.status_code == 200
    assert response.json()['decision'] == 'skip'

    response = client.delete(
        '/api/v1/recording-sessions/1/submission-settings', headers=headers
    )
    assert response.status_code == 200
    assert response.json()['inherited'] is True


def test_approved_archive_with_disabled_danmaku_exposes_manual_backfill(
    client: TestClient,
) -> None:
    fake = recording_sessions.journal
    assert isinstance(fake, FakeJournal)
    fake.upload_job_state = 'approved'
    fake.danmaku_branch_state = 'disabled'
    fake.upload_part_cid = 456

    response = client.get(
        '/api/v1/recording-sessions?limit=20&offset=40',
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 200
    actions = response.json()['sessions'][0]['availableActions']
    assert 'backfill_danmaku' in actions
    assert 'repair_transcode' not in actions


def test_retry_all_failed_upload_jobs_returns_durable_operation_admission(
    client: TestClient,
) -> None:
    actions = recording_sessions.task_actions
    assert actions is not None
    actions.admit_retry_all_failed.return_value = SimpleNamespace(
        operation_id='retry-operation-1', status='accepted', total=201
    )

    response = client.post(
        '/api/v1/recording-sessions/upload-jobs/retry-failed',
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 202
    assert response.json() == {
        'operationId': 'retry-operation-1',
        'status': 'accepted',
        'total': 201,
    }
    assert response.headers['X-BLREC-Operation-ID'] == 'retry-operation-1'
    actions.admit_retry_all_failed.assert_awaited_once()
    assert actions.admit_retry_all_failed.await_args.kwargs['manager_subject']


def test_unavailable_journal_returns_503(client: TestClient) -> None:
    recording_sessions.journal = None
    recording_sessions.unavailable_reason = 'upload database is unavailable'

    response = client.get(
        '/api/v1/recording-sessions', headers={'x-api-key': 'test-api-key'}
    )

    assert response.status_code == 503
    assert response.json()['detail'] == 'upload database is unavailable'


def test_upload_job_actions_return_partial_batch_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.web.routers.recording_sessions.audit',
        lambda event, **fields: audit_events.append((event, fields)),
    )
    actions = recording_sessions.task_actions
    assert isinstance(actions, AsyncMock)
    actions.run_job_batch.return_value = (
        SimpleNamespace(target_id=9, accepted=True, message='失败任务已重新排队'),
        SimpleNamespace(
            target_id=10, accepted=False, message='投稿结果未知，不能自动重试'
        ),
    )

    response = client.post(
        '/api/v1/recording-sessions/upload-jobs/actions',
        headers={'x-api-key': 'test-api-key'},
        json={'action': 'retry_failed', 'jobIds': [9, 10]},
    )

    assert response.status_code == 200
    assert response.json() == {
        'results': [
            {'jobId': 9, 'accepted': True, 'message': '失败任务已重新排队'},
            {'jobId': 10, 'accepted': False, 'message': '投稿结果未知，不能自动重试'},
        ]
    }
    actions.run_job_batch.assert_awaited_once()
    assert actions.run_job_batch.await_args.args == ('retry_failed', [9, 10])
    assert actions.run_job_batch.await_args.kwargs['manager_subject']
    assert audit_events == [
        (
            'upload_task_action',
            {
                'level': 'WARNING',
                'action': 'retry_failed',
                'job_ids': [9, 10],
                'accepted': 1,
                'rejected': 1,
            },
        )
    ]


def test_upload_job_actions_validate_nonempty_unique_batch(client: TestClient) -> None:
    empty = client.post(
        '/api/v1/recording-sessions/upload-jobs/actions',
        headers={'x-api-key': 'test-api-key'},
        json={'action': 'repair_transcode', 'jobIds': []},
    )
    duplicate = client.post(
        '/api/v1/recording-sessions/upload-jobs/actions',
        headers={'x-api-key': 'test-api-key'},
        json={'action': 'repair_transcode', 'jobIds': [9, 9]},
    )

    assert empty.status_code == 422
    assert duplicate.status_code == 422


def test_recording_session_actions_work_without_an_upload_job(
    client: TestClient,
) -> None:
    runner = recording_sessions.session_batch_runner
    assert isinstance(runner, AsyncMock)
    runner.return_value = (
        SimpleNamespace(
            target_id=1, accepted=True, message='本场录像将在文件就绪后上传'
        ),
        SimpleNamespace(target_id=2, accepted=False, message='录制场次不存在'),
    )

    response = client.post(
        '/api/v1/recording-sessions/actions',
        headers={'x-api-key': 'test-api-key'},
        json={'action': 'set_upload', 'sessionIds': [1, 2]},
    )

    assert response.status_code == 200
    assert response.json() == {
        'results': [
            {'sessionId': 1, 'accepted': True, 'message': '本场录像将在文件就绪后上传'},
            {'sessionId': 2, 'accepted': False, 'message': '录制场次不存在'},
        ]
    }
    runner.assert_awaited_once()
    assert runner.await_args.args == ('set_upload', [1, 2])
    assert runner.await_args.kwargs['manager_subject']


def test_recording_session_actions_forward_manual_danmaku_backfill(
    client: TestClient,
) -> None:
    runner = recording_sessions.session_batch_runner
    assert isinstance(runner, AsyncMock)
    runner.return_value = (
        SimpleNamespace(
            target_id=1, accepted=True, message='已排队回灌 1 个分 P 的弹幕'
        ),
    )

    response = client.post(
        '/api/v1/recording-sessions/actions',
        headers={'x-api-key': 'test-api-key'},
        json={'action': 'backfill_danmaku', 'sessionIds': [1]},
    )

    assert response.status_code == 200
    assert response.json()['results'] == [
        {'sessionId': 1, 'accepted': True, 'message': '已排队回灌 1 个分 P 的弹幕'}
    ]
    runner.assert_awaited_once()
    assert runner.await_args.args == ('backfill_danmaku', [1])
    assert runner.await_args.kwargs['manager_subject']


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


def test_completed_media_supports_etag_and_conditional_range(
    client: TestClient,
) -> None:
    current = recording_sessions.content_reader
    assert isinstance(current, FakeContentReader)
    recording_sessions.content_reader = FakeContentReader(
        current.media_path, recording=False
    )

    initial = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        headers={'x-api-key': 'test-api-key'},
    )
    etag = initial.headers['etag']
    not_modified = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        headers={
            'x-api-key': 'test-api-key',
            'If-None-Match': etag,
            'Range': 'bytes=2-4',
        },
    )
    matched_range = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        headers={'x-api-key': 'test-api-key', 'If-Range': etag, 'Range': 'bytes=2-4'},
    )
    stale_range = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        headers={
            'x-api-key': 'test-api-key',
            'If-Range': '"stale"',
            'Range': 'bytes=2-4',
        },
    )

    assert initial.status_code == 200
    assert initial.headers['cache-control'] == 'private, max-age=3600'
    assert not_modified.status_code == 304
    assert not_modified.content == b''
    assert matched_range.status_code == 206
    assert matched_range.content == b'234'
    assert stale_range.status_code == 200
    assert stale_range.content == b'0123456789'


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
    assert access.json()['playbackMode'] == 'active_snapshot'
    assert access.json()['indexState'] == 'pending'
    assert access.json()['retryAfterMs'] is None
    assert access.json()['requestId']
    assert 'test-api-key' not in access.text

    response = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        params={
            'media_token': access.json()['token'],
            'media_expires': access.json()['expiresAt'],
            'media_snapshot': access.json()['snapshotId'],
        },
        headers={'range': 'bytes=5-'},
    )
    assert response.status_code == 206
    assert response.content == b'56789'


def test_recording_thumbnail_route_is_not_exposed(client: TestClient) -> None:
    response = client.get('/api/v1/recording-sessions/parts/2/thumbnail')

    assert response.status_code == 404


def test_media_access_builds_a_seekable_snapshot_for_a_growing_flv(
    client: TestClient, tmp_path: Path
) -> None:
    media = tmp_path / 'growing.flv'
    original = BytesIO()
    writer = FlvWriter(original)
    writer.write_header(FlvHeader('FLV', 1, 5, 9))
    writer.write_tag(create_metadata_tag({'duration': 0.0, 'filesize': 0.0}))
    tail_start = original.tell()
    tail = b'video-tag-0' + b'video-tag-1' + b'video-tag-2'
    original.write(tail)
    media.write_bytes(original.getvalue())

    class SnapshotContentReader(FakeContentReader):
        async def media(self, part_id: int) -> MediaResource:
            assert part_id == 2
            media_stat = media.stat()
            return MediaResource(
                path=str(media),
                size=media.stat().st_size,
                content_type='video/x-flv',
                recording=True,
                room_id=100,
                part_index=1,
                bvid=None,
                remote_available=False,
                playback_mode='active_snapshot',
                index_state='pending',
                source_device=media_stat.st_dev,
                source_inode=media_stat.st_ino,
            )

    recording_sessions.content_reader = SnapshotContentReader(media)
    recording_sessions.active_recording_metadata_provider = lambda resource: {
        'duration': 12.5,
        'filesize': float(resource.size or 0),
        'keyframes': {
            'times': [0.0, 5.0, 10.0],
            'filepositions': [
                float(tail_start),
                float(tail_start + 11),
                float(tail_start + 22),
            ],
        },
    }

    access = client.post(
        '/api/v1/recording-sessions/parts/2/media-access',
        headers={'x-api-key': 'test-api-key'},
    )

    assert access.status_code == 200
    body = access.json()
    assert body['snapshotId']
    assert body['durationMs'] == 12_500
    assert body['fileSizeBytes'] > media.stat().st_size
    assert body['recording'] is True
    assert body['playbackMode'] == 'active_snapshot'
    service = recording_sessions.active_media_service
    assert isinstance(service, FakeActiveMediaService)
    assert len(service.calls) == 1
    assert service.calls[0][:3] == (2, str(media), media.stat().st_size)

    response = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        params={
            'media_token': body['token'],
            'media_expires': body['expiresAt'],
            'media_snapshot': body['snapshotId'],
        },
    )
    assert response.status_code == 200
    assert len(response.content) == body['fileSizeBytes']
    assert response.headers['cache-control'] == 'no-store'
    reader = FlvReader(BytesIO(response.content))
    reader.read_header()
    metadata = parse_metadata(reader.read_tag())
    assert metadata['duration'] == 12.5
    assert metadata['filesize'] == body['fileSizeBytes']


def test_media_access_freezes_a_growing_flv_when_index_creation_fails(
    client: TestClient, tmp_path: Path
) -> None:
    media = tmp_path / 'unindexed.flv'
    opened_content = b'FLV-opened-content'
    media.write_bytes(opened_content)

    class GrowingContentReader(FakeContentReader):
        async def media(self, part_id: int) -> MediaResource:
            assert part_id == 2
            media_stat = media.stat()
            return MediaResource(
                path=str(media),
                size=media.stat().st_size,
                content_type='video/x-flv',
                recording=True,
                room_id=100,
                part_index=1,
                bvid=None,
                remote_available=False,
                playback_mode='active_snapshot',
                index_state='pending',
                source_device=media_stat.st_dev,
                source_inode=media_stat.st_ino,
            )

    recording_sessions.content_reader = GrowingContentReader(media)
    recording_sessions.active_recording_metadata_provider = lambda _resource: {}

    access = client.post(
        '/api/v1/recording-sessions/parts/2/media-access',
        headers={'x-api-key': 'test-api-key'},
    )

    assert access.status_code == 200
    body = access.json()
    assert body['snapshotId']
    assert body['durationMs'] is None
    assert body['fileSizeBytes'] == len(opened_content)
    media.write_bytes(opened_content + b'-appended-later')

    response = client.get(
        '/api/v1/recording-sessions/parts/2/media',
        params={
            'media_token': body['token'],
            'media_expires': body['expiresAt'],
            'media_snapshot': body['snapshotId'],
        },
    )
    assert response.status_code == 200
    assert response.headers['content-length'] == str(len(opened_content))
    assert response.content == opened_content


def test_media_access_returns_retry_after_when_active_media_is_busy(
    client: TestClient,
) -> None:
    service = recording_sessions.active_media_service
    assert isinstance(service, FakeActiveMediaService)
    service.error = ActiveMediaBusy(retry_after=1)
    recording_sessions.active_recording_metadata_provider = lambda _resource: {
        'lastkeyframelocation': 1.0,
        'lastkeyframetimestamp': 1.0,
    }

    response = client.post(
        '/api/v1/recording-sessions/parts/2/media-access',
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 503
    assert response.headers['retry-after'] == '1'
    assert response.json()['detail'] == '活动视频快照暂时繁忙，请稍后重试'


def test_media_snapshot_store_keeps_only_the_latest_64_tokens() -> None:
    store = recording_sessions.MediaSnapshotStore()
    snapshots = []
    for part_id in range(65):
        snapshot = FlvMediaSnapshot.frozen('/rec/{}.flv'.format(part_id), part_id)
        token = store.add(part_id, 2**31, snapshot)
        snapshots.append((part_id, token))

    assert store.get(*snapshots[0]) is None
    assert all(store.get(*item) is not None for item in snapshots[1:])


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
                'user': '主播',
                'uid': 42,
                'content': '第一条',
            },
            {
                'index': 4,
                'progressMs': 2_500,
                'mode': 4,
                'fontSize': 18,
                'color': 255,
                'user': None,
                'uid': None,
                'content': '<script>不会执行</script>',
            },
        ],
        'nextCursor': 5,
    }


def test_danmaku_rejects_pages_over_five_hundred(client: TestClient) -> None:
    response = client.get(
        '/api/v1/recording-sessions/parts/2/danmaku?cursor=0&limit=501',
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 422


def test_danmaku_cursor_stale_returns_a_fixed_redacted_conflict(
    client: TestClient,
) -> None:
    response = client.get(
        '/api/v1/recording-sessions/parts/409/danmaku?cursor=100000&limit=2',
        headers={'x-api-key': 'test-api-key'},
    )

    assert response.status_code == 409
    assert response.json() == {'detail': '弹幕分页状态已失效，请从第一页重新加载'}
    assert 'private' not in response.text
    assert '100000' not in response.text
