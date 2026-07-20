import os
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple, Union

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, Field, validator
from starlette.responses import Response

from blrec.bili_upload.active_media import ActiveMediaBusy, ActiveMediaService
from blrec.bili_upload.journal import (
    DanmakuItemProgress,
    RecordingJournalBridge,
    RecordingPart,
    RecordingSession,
    RecordingSessionSummary,
    UploadJobProgress,
    UploadJobSummary,
    UploadPartProgress,
)
from blrec.bili_upload.policies import InvalidRoomUploadPolicy
from blrec.bili_upload.recording_content import (
    DanmakuPage,
    FlvMediaSnapshot,
    MediaResource,
    RecordingContentCursorStale,
    RecordingContentInvalid,
    RecordingContentNotFound,
    RecordingContentReader,
    RecordingContentUnavailable,
)
from blrec.bili_upload.session_submission import (
    InvalidSessionSubmission,
    RecordingSessionNotFound,
    SessionSubmissionLocked,
    SessionSubmissionManager,
    SessionSubmissionView,
)
from blrec.bili_upload.task_actions import (
    UploadTaskActionManager,
    UploadTaskActionRejected,
)
from blrec.logging.audit import audit
from blrec.utils.string import camel_case
from blrec.web.media_response import (
    MediaCandidate,
    MediaResourceUnavailable,
    VirtualMediaSnapshot,
    build_media_response,
    open_media_resource,
)

from .. import security
from .bili_accounts import authenticated_manager_subject
from .room_upload_policies import RoomUploadPolicyRequest

journal: Optional[RecordingJournalBridge] = None
content_reader: Optional[RecordingContentReader] = None
task_actions: Optional[UploadTaskActionManager] = None
session_action_runner: Optional[Callable[..., Awaitable[str]]] = None
submission_manager: Optional[SessionSubmissionManager] = None
active_recording_metadata_provider: Optional[
    Callable[[MediaResource], Optional[object]]
] = None
active_media_service: Optional[ActiveMediaService] = None
unavailable_reason: Optional[str] = 'Recording journal is not ready'

_MEDIA_ACCESS_TTL_SECONDS = 2 * 60 * 60
_MAX_MEDIA_SNAPSHOTS = 64


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class RecordingPartResponse(ApiModel):
    id: int
    run_id: str
    part_index: int
    source_path: str
    final_path: Optional[str]
    xml_path: Optional[str]
    record_start_time: int
    record_end_time: Optional[int]
    record_duration_seconds: Optional[int]
    file_size_bytes: Optional[int]
    danmaku_count: int
    artifact_state: str
    xml_completed: bool
    source_exists: bool
    final_exists: bool
    error_message: Optional[str]
    upload_excluded_reason: Optional[str]
    media_index_state: str
    media_index_error: Optional[str]
    media_index_progress: float


class UploadPartProgressResponse(ApiModel):
    id: int
    part_index: int
    upload_state: str
    danmaku_import_state: str
    remote_filename: Optional[str]
    cid: Optional[int]
    transcode_state: str
    transcode_fail_code: Optional[int]
    transcode_fail_desc: Optional[str]
    repair_stage: str
    repair_diagnostic: Optional[str]
    confirmed_bytes: int
    total_bytes: int


class DanmakuItemProgressResponse(ApiModel):
    id: int
    part_index: int
    progress_ms: int
    content: str
    error_message: Optional[str]


class UploadJobProgressResponse(ApiModel):
    id: int
    account_id: int
    account_uid: int
    account_display_name: str
    state: str
    submit_state: str
    preupload_finalized: bool
    display_state: Literal[
        'standard', 'preuploading', 'preuploaded_waiting', 'preupload_paused'
    ]
    comment_branch_state: str
    danmaku_branch_state: str
    aid: Optional[int]
    bvid: Optional[str]
    review_reason: Optional[str]
    attempt: int
    next_attempt_at: int
    created_at: int
    updated_at: int
    danmaku_total: int
    danmaku_confirmed: int
    danmaku_pending: int
    danmaku_unknown: int
    danmaku_failed: int
    repair_state: str
    repair_message: Optional[str]
    repair_error: Optional[str]
    can_retry: bool
    can_repair: bool
    can_skip: bool
    can_repost: bool
    can_delete: bool
    operator_paused: bool
    scheduled_publish_at: Optional[int]
    collection_branch_state: str
    collection_error: Optional[str]
    submission_verification_state: str
    submission_verified_at: Optional[int]
    submission_verification: Optional[Dict[str, object]]
    comment_error: Optional[str]
    danmaku_error: Optional[str]
    can_pause: bool
    can_resume: bool
    can_edit: bool
    confirmed_bytes: int
    total_bytes: int
    percent: float
    bytes_per_second: Optional[float]
    eta_seconds: Optional[int]
    current_part_index: Optional[int]
    confirmed_part_count: int
    discovered_part_count: int
    unknown_danmaku_items: List[DanmakuItemProgressResponse]
    parts: List[UploadPartProgressResponse]


class UploadJobSummaryResponse(ApiModel):
    id: int
    account_id: int
    account_uid: int
    account_display_name: str
    state: str
    submit_state: str
    preupload_finalized: bool
    display_state: Literal[
        'standard', 'preuploading', 'preuploaded_waiting', 'preupload_paused'
    ]
    comment_branch_state: str
    danmaku_branch_state: str
    aid: Optional[int]
    bvid: Optional[str]
    review_reason: Optional[str]
    attempt: int
    next_attempt_at: int
    created_at: int
    updated_at: int
    danmaku_total: int
    danmaku_confirmed: int
    danmaku_pending: int
    danmaku_unknown: int
    danmaku_failed: int
    repair_state: str
    repair_message: Optional[str]
    repair_error: Optional[str]
    can_retry: bool
    can_repair: bool
    can_skip: bool
    can_repost: bool
    can_delete: bool
    operator_paused: bool
    scheduled_publish_at: Optional[int]
    collection_branch_state: str
    collection_error: Optional[str]
    submission_verification_state: str
    submission_verified_at: Optional[int]
    comment_error: Optional[str]
    danmaku_error: Optional[str]
    can_pause: bool
    can_resume: bool
    can_edit: bool
    confirmed_bytes: int
    total_bytes: int
    percent: float
    bytes_per_second: Optional[float]
    eta_seconds: Optional[int]
    current_part_index: Optional[int]
    confirmed_part_count: int
    discovered_part_count: int


class UploadJobActionRequest(ApiModel):
    action: Literal[
        'retry_failed',
        'repair_transcode',
        'skip_upload',
        'repost_as_new',
        'delete_local',
        'pause_upload',
        'resume_upload',
    ]
    job_ids: List[int] = Field(..., min_items=1, max_items=100)

    @validator('job_ids')
    def job_ids_must_be_unique(cls, value: List[int]) -> List[int]:
        if any(job_id <= 0 for job_id in value):
            raise ValueError('job IDs must be positive')
        if len(set(value)) != len(value):
            raise ValueError('job IDs must be unique')
        return value


class UploadJobActionResultResponse(ApiModel):
    job_id: int
    accepted: bool
    message: str


class UploadJobActionResponse(ApiModel):
    results: List[UploadJobActionResultResponse]


class RecordingSessionActionRequest(ApiModel):
    action: Literal[
        'set_upload',
        'set_skip',
        'retry_failed',
        'repair_transcode',
        'backfill_danmaku',
        'repost_as_new',
        'delete_local',
        'pause_upload',
        'resume_upload',
    ]
    session_ids: List[int] = Field(..., min_items=1, max_items=100)

    @validator('session_ids')
    def session_ids_must_be_unique(cls, value: List[int]) -> List[int]:
        if any(session_id <= 0 for session_id in value):
            raise ValueError('session IDs must be positive')
        if len(set(value)) != len(value):
            raise ValueError('session IDs must be unique')
        return value


class RecordingSessionActionResultResponse(ApiModel):
    session_id: int
    accepted: bool
    message: str


class RecordingSessionActionResponse(ApiModel):
    results: List[RecordingSessionActionResultResponse]


class UploadJobRetryPreviewItemResponse(ApiModel):
    job_id: int
    room_id: int
    title: str
    account_display_name: str
    reason: str


class UploadJobRetryPreviewResponse(ApiModel):
    items: List[UploadJobRetryPreviewItemResponse]


class UploadTaskSettingsResponse(ApiModel):
    job_id: int
    account_id: int
    settings: Dict[str, Any]
    editable: bool
    blocked_reason: Optional[str]


class UploadTaskSettingsUpdateRequest(ApiModel):
    account_id: int = Field(..., gt=0)
    changes: Dict[str, Any]

    @validator('changes')
    def changes_must_not_be_empty(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        if not value or len(value) > 30:
            raise ValueError('changes must contain 1 to 30 fields')
        return value


class UploadTaskSettingsUpdateResponse(ApiModel):
    collection_cleared: bool
    task: UploadTaskSettingsResponse


class RecordingSessionResponse(ApiModel):
    id: int
    room_id: int
    broadcast_session_key: str
    live_start_time: Optional[int]
    state: str
    started_at: int
    ended_at: Optional[int]
    title: str
    cover_url: str
    cover_path: Optional[str]
    anchor_uid: Optional[int]
    anchor_name: str
    area_id: Optional[int]
    area_name: str
    parent_area_id: Optional[int]
    parent_area_name: str
    live_end_time: Optional[int]
    part_count: int
    danmaku_count: int
    total_file_size_bytes: int
    record_duration_seconds: int
    upload_intent: str
    upload_decision: str
    submission_inherited: bool
    upload_resolution_state: str
    upload_resolution_error: Optional[str]
    upload_suppressed: bool
    deletion_state: str
    deletion_error: Optional[str]
    source_kind: str
    highlight_clip_id: Optional[int]
    display_state: str
    available_actions: List[str]
    upload_job: Optional[UploadJobProgressResponse]
    parts: List[RecordingPartResponse]


class RecordingSessionSummaryResponse(ApiModel):
    id: int
    room_id: int
    live_start_time: Optional[int]
    state: str
    started_at: int
    ended_at: Optional[int]
    title: str
    cover_url: str
    anchor_uid: Optional[int]
    anchor_name: str
    area_id: Optional[int]
    area_name: str
    parent_area_id: Optional[int]
    parent_area_name: str
    live_end_time: Optional[int]
    part_count: int
    danmaku_count: int
    total_file_size_bytes: int
    record_duration_seconds: int
    upload_intent: str
    upload_decision: str
    submission_inherited: bool
    upload_resolution_state: str
    upload_resolution_error: Optional[str]
    upload_suppressed: bool
    deletion_state: str
    deletion_error: Optional[str]
    source_kind: str
    highlight_clip_id: Optional[int]
    display_state: str
    available_actions: List[str]
    upload_job: Optional[UploadJobSummaryResponse]


class RecordingSessionsResponse(ApiModel):
    degraded_reason: Optional[str]
    total: int
    sessions: List[RecordingSessionSummaryResponse]


class SessionSubmissionSettingsResponse(ApiModel):
    session_id: int
    room_id: int
    decision: str
    inherited: bool
    settings_source: str
    resolution_state: str
    resolution_error: Optional[str]
    settings: RoomUploadPolicyRequest


class SessionSubmissionDecisionRequest(ApiModel):
    decision: Literal['follow_room', 'upload', 'skip']


class DanmakuLineResponse(ApiModel):
    index: int
    progress_ms: int
    mode: int
    font_size: int
    color: int
    user: Optional[str]
    uid: Optional[int]
    content: str


class DanmakuPageResponse(ApiModel):
    items: List[DanmakuLineResponse]
    next_cursor: Optional[int]


class MediaAccessResponse(ApiModel):
    token: str
    expires_at: int
    snapshot_id: Optional[str]
    duration_ms: Optional[int]
    file_size_bytes: int
    recording: bool
    playback_mode: str
    index_state: str
    retry_after_ms: Optional[int]
    request_id: str


class MediaSnapshotStore:
    def __init__(self) -> None:
        self._items: Dict[str, Tuple[int, int, FlvMediaSnapshot]] = {}

    def add(self, part_id: int, expires_at: int, snapshot: FlvMediaSnapshot) -> str:
        self._discard_expired()
        while len(self._items) >= _MAX_MEDIA_SNAPSHOTS:
            oldest = min(self._items, key=lambda key: self._items[key][1])
            del self._items[oldest]
        snapshot_id = secrets.token_urlsafe(18)
        self._items[snapshot_id] = (int(part_id), int(expires_at), snapshot)
        return snapshot_id

    def get(self, part_id: int, snapshot_id: str) -> Optional[FlvMediaSnapshot]:
        self._discard_expired()
        item = self._items.get(snapshot_id)
        if item is None or item[0] != int(part_id):
            return None
        return item[2]

    def clear(self) -> None:
        self._items.clear()

    def _discard_expired(self) -> None:
        now = int(time.time())
        for snapshot_id, (_, expires_at, _) in tuple(self._items.items()):
            if expires_at < now:
                del self._items[snapshot_id]


media_snapshot_store = MediaSnapshotStore()


def get_recording_journal() -> RecordingJournalBridge:
    if journal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Recording journal is unavailable',
        )
    return journal


def get_content_reader() -> RecordingContentReader:
    if content_reader is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Recording content is unavailable',
        )
    return content_reader


def get_task_actions() -> UploadTaskActionManager:
    if task_actions is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Upload task actions are unavailable',
        )
    return task_actions


def get_session_action_runner() -> Callable[..., Awaitable[str]]:
    if session_action_runner is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Recording session actions are unavailable',
        )
    return session_action_runner


def get_submission_manager() -> SessionSubmissionManager:
    if submission_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason
            or 'Recording submission settings are unavailable',
        )
    return submission_manager


def _session_display(
    session: Union[RecordingSession, RecordingSessionSummary],
    upload_job: Optional[Union[UploadJobProgress, UploadJobSummary]],
) -> Tuple[str, List[str]]:
    actions: List[str] = []
    if session.deletion_state in ('requested', 'deleting'):
        return 'deleting', actions
    if session.deletion_state == 'failed':
        return 'delete_failed', ['delete_local']
    if upload_job is not None:
        if upload_job.can_retry:
            actions.append('retry_failed')
        if upload_job.can_skip:
            actions.append('set_skip')
        if upload_job.can_repost:
            actions.append('repost_as_new')
        if upload_job.can_pause:
            actions.append('pause_upload')
        if upload_job.can_resume:
            actions.append('resume_upload')
        if upload_job.can_edit:
            actions.append('edit_task')
        can_backfill_danmaku = False
        if isinstance(upload_job, UploadJobSummary):
            can_backfill_danmaku = upload_job.can_backfill_danmaku
        elif isinstance(session, RecordingSession):
            recorded_part_indexes = {
                part.part_index
                for part in session.parts
                if part.xml_path and part.xml_completed
            }
            upload_part_indexes = {
                part.part_index for part in upload_job.parts if part.cid is not None
            }
            can_backfill_danmaku = (
                upload_job.state in ('approved', 'completed')
                and upload_job.danmaku_branch_state == 'disabled'
                and bool(recorded_part_indexes)
                and recorded_part_indexes == upload_part_indexes
            )
        if can_backfill_danmaku:
            actions.append('backfill_danmaku')
    elif session.upload_decision != 'skip' and session.upload_resolution_state != (
        'not_requested'
    ):
        actions.append('set_skip')
    else:
        actions.append('set_upload')
    if upload_job is None or not upload_job.preupload_finalized:
        actions.append('edit_submission')
    actions.append('delete_local')

    if session.state == 'open':
        return 'recording', actions
    if upload_job is None:
        if session.upload_resolution_state == 'configuration_required':
            return 'needs_attention', actions
        if session.upload_resolution_state == 'pending' and (
            session.upload_decision != 'skip'
        ):
            return 'pending_upload', actions
        return 'not_uploading', actions
    if upload_job.repair_state in ('failed', 'unknown_outcome'):
        return 'needs_attention', actions
    if upload_job.operator_paused:
        return 'paused', actions
    if upload_job.repair_state in ('queued', 'checking', 'reuploading', 'editing'):
        return 'uploading', actions
    if upload_job.repair_state == 'waiting_review':
        return 'waiting_review', actions
    if upload_job.state in ('waiting_artifacts', 'ready'):
        return 'pending_upload', actions
    if upload_job.state in ('uploading', 'submitting'):
        return 'uploading', actions
    if upload_job.state == 'waiting_review':
        return 'waiting_review', actions
    if upload_job.state in ('approved', 'completed'):
        return 'completed', actions
    return 'needs_attention', actions


def _content_error(error: RuntimeError) -> HTTPException:
    if isinstance(error, RecordingContentNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


async def authenticated_media_subject(
    request: Request,
    part_id: int,
    media_token: Optional[str] = Query(None),
    media_expires: Optional[int] = Query(None),
    media_snapshot: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None),
) -> str:
    if media_token is not None or media_expires is not None:
        if (
            media_token is not None
            and media_expires is not None
            and security.valid_media_access(
                part_id, media_expires, media_token, media_snapshot
            )
        ):
            return 'recording-media-access'
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail='播放凭据无效或已过期'
        )
    return await authenticated_manager_subject(request, x_api_key)


def _part_response(part: RecordingPart) -> RecordingPartResponse:
    return RecordingPartResponse(
        id=part.id,
        run_id=part.run_id,
        part_index=part.part_index,
        source_path=part.source_path,
        final_path=part.final_path,
        xml_path=part.xml_path,
        record_start_time=part.record_start_time,
        record_end_time=part.record_end_time,
        record_duration_seconds=part.record_duration_seconds,
        file_size_bytes=part.file_size_bytes,
        danmaku_count=part.danmaku_count,
        artifact_state=part.artifact_state,
        xml_completed=part.xml_completed,
        source_exists=part.source_exists,
        final_exists=part.final_exists,
        error_message=part.error_message,
        upload_excluded_reason=part.upload_excluded_reason,
        media_index_state=part.media_index_state,
        media_index_error=part.media_index_error,
        media_index_progress=part.media_index_progress,
    )


def _upload_part_response(part: UploadPartProgress) -> UploadPartProgressResponse:
    return UploadPartProgressResponse(
        id=part.id,
        part_index=part.part_index,
        upload_state=part.upload_state,
        danmaku_import_state=part.danmaku_import_state,
        remote_filename=part.remote_filename,
        cid=part.cid,
        transcode_state=part.transcode_state,
        transcode_fail_code=part.transcode_fail_code,
        transcode_fail_desc=part.transcode_fail_desc,
        repair_stage=part.repair_stage,
        repair_diagnostic=part.repair_diagnostic,
        confirmed_bytes=part.confirmed_bytes,
        total_bytes=part.total_bytes,
    )


def _danmaku_item_response(item: DanmakuItemProgress) -> DanmakuItemProgressResponse:
    return DanmakuItemProgressResponse(
        id=item.id,
        part_index=item.part_index,
        progress_ms=item.progress_ms,
        content=item.content,
        error_message=item.error_message,
    )


def _upload_job_response(job: UploadJobProgress) -> UploadJobProgressResponse:
    return UploadJobProgressResponse(
        id=job.id,
        account_id=job.account_id,
        account_uid=job.account_uid,
        account_display_name=job.account_display_name,
        state=job.state,
        submit_state=job.submit_state,
        preupload_finalized=job.preupload_finalized,
        display_state=job.display_state,
        comment_branch_state=job.comment_branch_state,
        danmaku_branch_state=job.danmaku_branch_state,
        aid=job.aid,
        bvid=job.bvid,
        review_reason=job.review_reason,
        attempt=job.attempt,
        next_attempt_at=job.next_attempt_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        danmaku_total=job.danmaku_total,
        danmaku_confirmed=job.danmaku_confirmed,
        danmaku_pending=job.danmaku_pending,
        danmaku_unknown=job.danmaku_unknown,
        danmaku_failed=job.danmaku_failed,
        repair_state=job.repair_state,
        repair_message=job.repair_message,
        repair_error=job.repair_error,
        can_retry=job.can_retry,
        can_repair=job.can_repair,
        can_skip=job.can_skip,
        can_repost=job.can_repost,
        can_delete=job.can_delete,
        operator_paused=job.operator_paused,
        scheduled_publish_at=job.scheduled_publish_at,
        collection_branch_state=job.collection_branch_state,
        collection_error=job.collection_error,
        submission_verification_state=job.submission_verification_state,
        submission_verified_at=job.submission_verified_at,
        submission_verification=job.submission_verification,
        comment_error=job.comment_error,
        danmaku_error=job.danmaku_error,
        can_pause=job.can_pause,
        can_resume=job.can_resume,
        can_edit=job.can_edit,
        confirmed_bytes=job.confirmed_bytes,
        total_bytes=job.total_bytes,
        percent=job.percent,
        bytes_per_second=job.bytes_per_second,
        eta_seconds=job.eta_seconds,
        current_part_index=job.current_part_index,
        confirmed_part_count=sum(
            part.upload_state == 'confirmed' for part in job.parts
        ),
        discovered_part_count=len(job.parts),
        unknown_danmaku_items=[
            _danmaku_item_response(item) for item in job.unknown_danmaku_items
        ],
        parts=[_upload_part_response(part) for part in job.parts],
    )


def _upload_job_summary_response(job: UploadJobSummary) -> UploadJobSummaryResponse:
    return UploadJobSummaryResponse(
        id=job.id,
        account_id=job.account_id,
        account_uid=job.account_uid,
        account_display_name=job.account_display_name,
        state=job.state,
        submit_state=job.submit_state,
        preupload_finalized=job.preupload_finalized,
        display_state=job.display_state,
        comment_branch_state=job.comment_branch_state,
        danmaku_branch_state=job.danmaku_branch_state,
        aid=job.aid,
        bvid=job.bvid,
        review_reason=job.review_reason,
        attempt=job.attempt,
        next_attempt_at=job.next_attempt_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        danmaku_total=job.danmaku_total,
        danmaku_confirmed=job.danmaku_confirmed,
        danmaku_pending=job.danmaku_pending,
        danmaku_unknown=job.danmaku_unknown,
        danmaku_failed=job.danmaku_failed,
        repair_state=job.repair_state,
        repair_message=job.repair_message,
        repair_error=job.repair_error,
        can_retry=job.can_retry,
        can_repair=job.can_repair,
        can_skip=job.can_skip,
        can_repost=job.can_repost,
        can_delete=job.can_delete,
        operator_paused=job.operator_paused,
        scheduled_publish_at=job.scheduled_publish_at,
        collection_branch_state=job.collection_branch_state,
        collection_error=job.collection_error,
        submission_verification_state=job.submission_verification_state,
        submission_verified_at=job.submission_verified_at,
        comment_error=job.comment_error,
        danmaku_error=job.danmaku_error,
        can_pause=job.can_pause,
        can_resume=job.can_resume,
        can_edit=job.can_edit,
        confirmed_bytes=job.confirmed_bytes,
        total_bytes=job.total_bytes,
        percent=job.percent,
        bytes_per_second=job.bytes_per_second,
        eta_seconds=job.eta_seconds,
        current_part_index=job.current_part_index,
        confirmed_part_count=job.confirmed_part_count,
        discovered_part_count=job.discovered_part_count,
    )


def _session_response(
    session: RecordingSession, upload_job: Optional[UploadJobProgress]
) -> RecordingSessionResponse:
    display_state, available_actions = _session_display(session, upload_job)
    return RecordingSessionResponse(
        id=session.id,
        room_id=session.room_id,
        broadcast_session_key=session.broadcast_session_key,
        live_start_time=session.live_start_time,
        state=session.state,
        started_at=session.started_at,
        ended_at=session.ended_at,
        title=(
            upload_job.title
            if session.source_kind == 'highlight'
            and upload_job is not None
            and upload_job.title
            else session.title
        ),
        cover_url=session.cover_url,
        cover_path=session.cover_path,
        anchor_uid=session.anchor_uid,
        anchor_name=session.anchor_name,
        area_id=session.area_id,
        area_name=session.area_name,
        parent_area_id=session.parent_area_id,
        parent_area_name=session.parent_area_name,
        live_end_time=session.live_end_time,
        part_count=session.part_count,
        danmaku_count=session.danmaku_count,
        total_file_size_bytes=session.total_file_size_bytes,
        record_duration_seconds=session.record_duration_seconds,
        upload_intent=session.upload_intent,
        upload_decision=session.upload_decision,
        submission_inherited=session.submission_inherited,
        upload_resolution_state=session.upload_resolution_state,
        upload_resolution_error=session.upload_resolution_error,
        upload_suppressed=session.upload_suppressed,
        deletion_state=session.deletion_state,
        deletion_error=session.deletion_error,
        source_kind=session.source_kind,
        highlight_clip_id=session.highlight_clip_id,
        display_state=display_state,
        available_actions=available_actions,
        upload_job=(None if upload_job is None else _upload_job_response(upload_job)),
        parts=[_part_response(part) for part in session.parts],
    )


def _session_summary_response(
    session: RecordingSessionSummary,
) -> RecordingSessionSummaryResponse:
    display_state, available_actions = _session_display(session, session.upload_job)
    return RecordingSessionSummaryResponse(
        id=session.id,
        room_id=session.room_id,
        live_start_time=session.live_start_time,
        state=session.state,
        started_at=session.started_at,
        ended_at=session.ended_at,
        title=session.title,
        cover_url=session.cover_url,
        anchor_uid=session.anchor_uid,
        anchor_name=session.anchor_name,
        area_id=session.area_id,
        area_name=session.area_name,
        parent_area_id=session.parent_area_id,
        parent_area_name=session.parent_area_name,
        live_end_time=session.live_end_time,
        part_count=session.part_count,
        danmaku_count=session.danmaku_count,
        total_file_size_bytes=session.total_file_size_bytes,
        record_duration_seconds=session.record_duration_seconds,
        upload_intent=session.upload_intent,
        upload_decision=session.upload_decision,
        submission_inherited=session.submission_inherited,
        upload_resolution_state=session.upload_resolution_state,
        upload_resolution_error=session.upload_resolution_error,
        upload_suppressed=session.upload_suppressed,
        deletion_state=session.deletion_state,
        deletion_error=session.deletion_error,
        source_kind=session.source_kind,
        highlight_clip_id=session.highlight_clip_id,
        display_state=display_state,
        available_actions=available_actions,
        upload_job=(
            None
            if session.upload_job is None
            else _upload_job_summary_response(session.upload_job)
        ),
    )


router = APIRouter(prefix='/recording-sessions', tags=['recording-sessions'])


@router.get('', response_model=RecordingSessionsResponse)
async def list_recording_sessions(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    scope: Literal['recordings', 'uploads'] = Query('recordings'),
    query: str = Query('', alias='q', max_length=100),
    session_state: Optional[
        Literal['open', 'closed', 'cancelled', 'manual_review', 'skipped']
    ] = Query(None, alias='recordingState'),
    upload_state: Optional[
        Literal[
            'waiting_artifacts',
            'ready',
            'uploading',
            'submitting',
            'waiting_review',
            'approved',
            'rejected',
            'paused',
            'completed',
            'none',
            'suppressed',
        ]
    ] = Query(None, alias='uploadState'),
    started_from: Optional[int] = Query(None, ge=0, alias='startedFrom'),
    started_to: Optional[int] = Query(None, ge=0, alias='startedTo'),
    sort_order: Literal['newest', 'oldest'] = Query('newest', alias='sort'),
    _subject: str = Depends(authenticated_manager_subject),
    recording_journal: RecordingJournalBridge = Depends(get_recording_journal),
) -> RecordingSessionsResponse:
    if (
        started_from is not None
        and started_to is not None
        and started_from > started_to
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='开始时间不能晚于结束时间',
        )
    normalized_query = query.strip()
    total = await recording_journal.count_sessions(
        scope=scope,
        query=normalized_query,
        session_state=session_state,
        upload_state=upload_state,
        started_from=started_from,
        started_to=started_to,
    )
    sessions = await recording_journal.list_session_summaries(
        limit=limit,
        offset=offset,
        scope=scope,
        query=normalized_query,
        session_state=session_state,
        upload_state=upload_state,
        started_from=started_from,
        started_to=started_to,
        sort_order=sort_order,
    )
    return RecordingSessionsResponse(
        degraded_reason=recording_journal.degraded_reason,
        total=total,
        sessions=[_session_summary_response(session) for session in sessions],
    )


@router.get('/{session_id}', response_model=RecordingSessionResponse)
async def get_recording_session(
    session_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    recording_journal: RecordingJournalBridge = Depends(get_recording_journal),
) -> RecordingSessionResponse:
    try:
        session = await recording_journal.get_session(session_id)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from None
    upload_jobs = await recording_journal.upload_jobs_for_sessions((session_id,))
    return _session_response(session, upload_jobs.get(session_id))


def _submission_settings_response(
    value: SessionSubmissionView,
) -> SessionSubmissionSettingsResponse:
    return SessionSubmissionSettingsResponse(
        session_id=value.session_id,
        room_id=value.room_id,
        decision=value.decision,
        inherited=value.inherited,
        settings_source=value.settings_source,
        resolution_state=value.resolution_state,
        resolution_error=value.resolution_error,
        settings=RoomUploadPolicyRequest(**value.settings.__dict__),
    )


@router.get(
    '/{session_id}/submission-settings',
    response_model=SessionSubmissionSettingsResponse,
)
async def get_session_submission_settings(
    session_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    manager: SessionSubmissionManager = Depends(get_submission_manager),
) -> SessionSubmissionSettingsResponse:
    try:
        return _submission_settings_response(await manager.get(session_id))
    except RecordingSessionNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


@router.put(
    '/{session_id}/submission-settings',
    response_model=SessionSubmissionSettingsResponse,
)
async def save_session_submission_settings(
    session_id: int,
    payload: RoomUploadPolicyRequest,
    subject: str = Depends(authenticated_manager_subject),
    manager: SessionSubmissionManager = Depends(get_submission_manager),
) -> SessionSubmissionSettingsResponse:
    try:
        value = await manager.save_override(
            session_id, payload.to_command(), manager_subject=subject
        )
    except RecordingSessionNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    except (
        InvalidRoomUploadPolicy,
        InvalidSessionSubmission,
        SessionSubmissionLocked,
    ) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return _submission_settings_response(value)


@router.delete(
    '/{session_id}/submission-settings',
    response_model=SessionSubmissionSettingsResponse,
)
async def clear_session_submission_settings(
    session_id: int,
    subject: str = Depends(authenticated_manager_subject),
    manager: SessionSubmissionManager = Depends(get_submission_manager),
) -> SessionSubmissionSettingsResponse:
    try:
        value = await manager.clear_override(session_id, manager_subject=subject)
    except RecordingSessionNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    except SessionSubmissionLocked as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return _submission_settings_response(value)


@router.patch(
    '/{session_id}/submission-decision',
    response_model=SessionSubmissionSettingsResponse,
)
async def set_session_submission_decision(
    session_id: int,
    payload: SessionSubmissionDecisionRequest,
    subject: str = Depends(authenticated_manager_subject),
    manager: SessionSubmissionManager = Depends(get_submission_manager),
) -> SessionSubmissionSettingsResponse:
    try:
        value = await manager.set_decision(
            session_id, payload.decision, manager_subject=subject
        )
    except RecordingSessionNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    except (InvalidSessionSubmission, SessionSubmissionLocked) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return _submission_settings_response(value)


@router.post('/upload-jobs/actions', response_model=UploadJobActionResponse)
async def run_upload_job_actions(
    command: UploadJobActionRequest,
    subject: str = Depends(authenticated_manager_subject),
    actions: UploadTaskActionManager = Depends(get_task_actions),
) -> UploadJobActionResponse:
    results = []
    for job_id in command.job_ids:
        try:
            if command.action == 'retry_failed':
                message = await actions.retry_failed(job_id, manager_subject=subject)
            elif command.action == 'repair_transcode':
                message = await actions.request_transcode_repair(
                    job_id, manager_subject=subject
                )
            elif command.action == 'skip_upload':
                message = await actions.skip_upload(job_id, manager_subject=subject)
            elif command.action == 'repost_as_new':
                message = await actions.repost_as_new(job_id, manager_subject=subject)
            elif command.action == 'pause_upload':
                message = await actions.pause_upload(job_id, manager_subject=subject)
            elif command.action == 'resume_upload':
                message = await actions.resume_upload(job_id, manager_subject=subject)
            else:
                message = await actions.delete_local_task(
                    job_id, manager_subject=subject
                )
        except UploadTaskActionRejected as error:
            results.append(
                UploadJobActionResultResponse(
                    job_id=job_id, accepted=False, message=str(error)
                )
            )
        else:
            results.append(
                UploadJobActionResultResponse(
                    job_id=job_id, accepted=True, message=message
                )
            )
    rejected = sum(not result.accepted for result in results)
    audit(
        'upload_task_action',
        level='WARNING' if rejected else 'INFO',
        action=command.action,
        job_ids=command.job_ids,
        accepted=len(results) - rejected,
        rejected=rejected,
    )
    return UploadJobActionResponse(results=results)


@router.get('/upload-jobs/{job_id}/settings', response_model=UploadTaskSettingsResponse)
async def get_upload_task_settings(
    job_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    actions: UploadTaskActionManager = Depends(get_task_actions),
) -> UploadTaskSettingsResponse:
    try:
        value = await actions.task_settings(job_id)
    except UploadTaskActionRejected as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    return UploadTaskSettingsResponse(
        job_id=value.job_id,
        account_id=value.account_id,
        settings=dict(value.settings),
        editable=value.editable,
        blocked_reason=value.blocked_reason,
    )


@router.put(
    '/upload-jobs/{job_id}/settings', response_model=UploadTaskSettingsUpdateResponse
)
async def update_upload_task_settings(
    job_id: int,
    command: UploadTaskSettingsUpdateRequest,
    subject: str = Depends(authenticated_manager_subject),
    actions: UploadTaskActionManager = Depends(get_task_actions),
) -> UploadTaskSettingsUpdateResponse:
    try:
        result = await actions.update_task(
            job_id,
            account_id=command.account_id,
            changes=command.changes,
            manager_subject=subject,
        )
        value = await actions.task_settings(job_id)
    except UploadTaskActionRejected as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    audit(
        'upload_task_settings_updated',
        job_id=job_id,
        account_id=command.account_id,
        changed_fields=sorted(command.changes),
        collection_cleared=result.collection_cleared,
    )
    return UploadTaskSettingsUpdateResponse(
        collection_cleared=result.collection_cleared,
        task=UploadTaskSettingsResponse(
            job_id=value.job_id,
            account_id=value.account_id,
            settings=dict(value.settings),
            editable=value.editable,
            blocked_reason=value.blocked_reason,
        ),
    )


@router.post('/actions', response_model=RecordingSessionActionResponse)
async def run_recording_session_actions(
    command: RecordingSessionActionRequest,
    subject: str = Depends(authenticated_manager_subject),
    runner: Callable[..., Awaitable[str]] = Depends(get_session_action_runner),
) -> RecordingSessionActionResponse:
    results = []
    for session_id in command.session_ids:
        try:
            message = await runner(command.action, session_id, manager_subject=subject)
        except UploadTaskActionRejected as error:
            results.append(
                RecordingSessionActionResultResponse(
                    session_id=session_id, accepted=False, message=str(error)
                )
            )
        else:
            results.append(
                RecordingSessionActionResultResponse(
                    session_id=session_id, accepted=True, message=message
                )
            )
    rejected = sum(not result.accepted for result in results)
    audit(
        'recording_session_action',
        level='WARNING' if rejected else 'INFO',
        action=command.action,
        session_ids=command.session_ids,
        accepted=len(results) - rejected,
        rejected=rejected,
    )
    return RecordingSessionActionResponse(results=results)


@router.post('/upload-jobs/retry-failed', response_model=UploadJobActionResponse)
async def retry_all_failed_upload_jobs(
    subject: str = Depends(authenticated_manager_subject),
    actions: UploadTaskActionManager = Depends(get_task_actions),
) -> UploadJobActionResponse:
    results = []
    for job_id in await actions.retryable_failed_job_ids():
        try:
            message = await actions.retry_failed(job_id, manager_subject=subject)
        except UploadTaskActionRejected as error:
            results.append(
                UploadJobActionResultResponse(
                    job_id=job_id, accepted=False, message=str(error)
                )
            )
        else:
            results.append(
                UploadJobActionResultResponse(
                    job_id=job_id, accepted=True, message=message
                )
            )
    rejected = sum(not result.accepted for result in results)
    audit(
        'upload_failed_jobs_retried',
        level='WARNING' if rejected else 'INFO',
        job_ids=[result.job_id for result in results],
        accepted=len(results) - rejected,
        rejected=rejected,
    )
    return UploadJobActionResponse(results=results)


@router.get(
    '/upload-jobs/retry-failed-preview', response_model=UploadJobRetryPreviewResponse
)
async def preview_retryable_failed_upload_jobs(
    _subject: str = Depends(authenticated_manager_subject),
    actions: UploadTaskActionManager = Depends(get_task_actions),
) -> UploadJobRetryPreviewResponse:
    return UploadJobRetryPreviewResponse(
        items=[
            UploadJobRetryPreviewItemResponse(
                job_id=item.job_id,
                room_id=item.room_id,
                title=item.title,
                account_display_name=item.account_display_name,
                reason=item.reason,
            )
            for item in await actions.retryable_failed_jobs()
        ]
    )


@router.post('/parts/{part_id}/media-access', response_model=MediaAccessResponse)
async def create_recording_media_access(
    part_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    reader: RecordingContentReader = Depends(get_content_reader),
) -> MediaAccessResponse:
    request_id = secrets.token_hex(8)
    started = time.monotonic()
    try:
        resource = await reader.media(part_id)
    except (RecordingContentNotFound, RecordingContentUnavailable) as error:
        audit(
            'media_access_failed',
            level='WARNING',
            request_id=request_id,
            part_id=part_id,
            elapsed_ms=int((time.monotonic() - started) * 1_000),
            error=str(error)[:500],
            result='failed',
        )
        raise _content_error(error) from None
    if resource.path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='该分 P 的本地视频不可用'
        )
    expires_at = int(time.time()) + _MEDIA_ACCESS_TTL_SECONDS
    snapshot_id = None
    duration_ms = None
    file_size_bytes = int(resource.size or 0)
    if (
        resource.recording
        and resource.content_type == 'video/x-flv'
        and resource.size is not None
    ):
        snapshot = FlvMediaSnapshot.frozen(
            os.path.abspath(resource.path),
            resource.size,
            source_device=resource.source_device,
            source_inode=resource.source_inode,
        )
        if active_recording_metadata_provider is not None:
            metadata = active_recording_metadata_provider(resource)
            if metadata is not None and active_media_service is not None:
                try:
                    snapshot = await active_media_service.snapshot(
                        part_id, resource.path, resource.size, metadata
                    )
                except ActiveMediaBusy as error:
                    audit(
                        'media_access_failed',
                        level='WARNING',
                        request_id=request_id,
                        part_id=part_id,
                        elapsed_ms=int((time.monotonic() - started) * 1_000),
                        error_code='active_media_busy',
                        result='busy',
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail='活动视频快照暂时繁忙，请稍后重试',
                        headers={'Retry-After': str(error.retry_after)},
                    ) from None
                except (OSError, EOFError, ValueError, AssertionError, RuntimeError):
                    pass
        snapshot_id = media_snapshot_store.add(part_id, expires_at, snapshot)
        duration_ms = snapshot.duration_ms
        file_size_bytes = snapshot.size
    response = MediaAccessResponse(
        token=security.media_access_token(part_id, expires_at, snapshot_id),
        expires_at=expires_at,
        snapshot_id=snapshot_id,
        duration_ms=duration_ms,
        file_size_bytes=file_size_bytes,
        recording=resource.recording,
        playback_mode=resource.playback_mode,
        index_state=resource.index_state,
        retry_after_ms=None,
        request_id=request_id,
    )
    audit(
        'media_access_completed',
        request_id=request_id,
        part_id=part_id,
        playback_mode=resource.playback_mode,
        index_state=resource.index_state,
        recording=resource.recording,
        file_size_bytes=file_size_bytes,
        elapsed_ms=int((time.monotonic() - started) * 1_000),
        result='completed',
    )
    return response


@router.get('/parts/{part_id}/media')
async def stream_recording_media(
    part_id: int,
    request: Request,
    range_header: Optional[str] = Header(None, alias='Range'),
    if_none_match: Optional[str] = Header(None, alias='If-None-Match'),
    if_range: Optional[str] = Header(None, alias='If-Range'),
    media_snapshot: Optional[str] = Query(None),
    _subject: str = Depends(authenticated_media_subject),
    reader: RecordingContentReader = Depends(get_content_reader),
) -> Response:
    try:
        descriptor = await reader.media_descriptor(part_id)
    except (RecordingContentNotFound, RecordingContentUnavailable) as error:
        raise _content_error(error) from None
    snapshot = None
    if media_snapshot is not None:
        snapshot = media_snapshot_store.get(part_id, media_snapshot)
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='播放快照已失效，请重新打开播放器',
            )
    virtual_snapshot = (
        None
        if snapshot is None
        else VirtualMediaSnapshot(
            path=snapshot.path,
            source_size=snapshot.source_size,
            source_device=(
                -1 if snapshot.source_device is None else snapshot.source_device
            ),
            source_inode=(
                -1 if snapshot.source_inode is None else snapshot.source_inode
            ),
            source_tail_start=snapshot.source_tail_start,
            prefix=snapshot.prefix,
        )
    )
    try:
        resource = await open_media_resource(
            tuple(
                MediaCandidate(
                    path=candidate.path,
                    content_type=candidate.content_type,
                    artifact_key=candidate.artifact_key,
                    active=candidate.recording,
                )
                for candidate in descriptor.candidates
            ),
            expected_root=descriptor.expected_root,
            snapshot=virtual_snapshot,
        )
    except MediaResourceUnavailable:
        detail = (
            '播放快照已失效，请重新打开播放器'
            if snapshot is not None
            else '该分 P 的本地视频不可用'
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=detail
        ) from None
    return build_media_response(
        request, resource, range_header, if_none_match, if_range, None
    )


@router.get('/parts/{part_id}/danmaku', response_model=DanmakuPageResponse)
async def list_recording_danmaku(
    part_id: int,
    cursor: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    _subject: str = Depends(authenticated_manager_subject),
    reader: RecordingContentReader = Depends(get_content_reader),
) -> DanmakuPageResponse:
    try:
        page: DanmakuPage = await reader.danmaku(part_id, cursor=cursor, limit=limit)
    except RecordingContentCursorStale:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='弹幕分页状态已失效，请从第一页重新加载',
        ) from None
    except (
        RecordingContentNotFound,
        RecordingContentUnavailable,
        RecordingContentInvalid,
    ) as error:
        raise _content_error(error) from None
    return DanmakuPageResponse(
        items=[
            DanmakuLineResponse(
                index=item.index,
                progress_ms=item.progress_ms,
                mode=item.mode,
                font_size=item.font_size,
                color=item.color,
                user=item.user,
                uid=item.uid,
                content=item.content,
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
    )
