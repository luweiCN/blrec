from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, Field, validator
from starlette.responses import Response

from blrec.bili_upload.danmaku_publish import DanmakuPublisher
from blrec.bili_upload.journal import (
    DanmakuItemProgress,
    RecordingJournalBridge,
    RecordingPart,
    RecordingSession,
    UploadJobProgress,
    UploadPartProgress,
)
from blrec.utils.string import camel_case

from .bili_accounts import authenticated_manager_subject

journal: Optional[RecordingJournalBridge] = None
danmaku_publisher: Optional[DanmakuPublisher] = None
unavailable_reason: Optional[str] = 'Recording journal is not enabled'


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


class UploadPartProgressResponse(ApiModel):
    id: int
    part_index: int
    upload_state: str
    danmaku_import_state: str
    remote_filename: Optional[str]
    cid: Optional[int]


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
    unknown_danmaku_items: List[DanmakuItemProgressResponse]
    parts: List[UploadPartProgressResponse]


class DanmakuDecisionRequest(ApiModel):
    action: Literal['assume_success', 'retry_accept_duplicate_risk']
    reason: str = Field(..., min_length=1, max_length=500)

    @validator('reason')
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError('reason must not be blank')
        return normalized


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
    upload_job: Optional[UploadJobProgressResponse]
    parts: List[RecordingPartResponse]


class RecordingSessionsResponse(ApiModel):
    degraded_reason: Optional[str]
    sessions: List[RecordingSessionResponse]


def get_recording_journal() -> RecordingJournalBridge:
    if journal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Recording journal is unavailable',
        )
    return journal


def get_danmaku_publisher() -> DanmakuPublisher:
    if danmaku_publisher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Danmaku backfill is unavailable',
        )
    return danmaku_publisher


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
    )


def _upload_part_response(part: UploadPartProgress) -> UploadPartProgressResponse:
    return UploadPartProgressResponse(
        id=part.id,
        part_index=part.part_index,
        upload_state=part.upload_state,
        danmaku_import_state=part.danmaku_import_state,
        remote_filename=part.remote_filename,
        cid=part.cid,
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
        unknown_danmaku_items=[
            _danmaku_item_response(item) for item in job.unknown_danmaku_items
        ],
        parts=[_upload_part_response(part) for part in job.parts],
    )


def _session_response(
    session: RecordingSession, upload_job: Optional[UploadJobProgress]
) -> RecordingSessionResponse:
    return RecordingSessionResponse(
        id=session.id,
        room_id=session.room_id,
        broadcast_session_key=session.broadcast_session_key,
        live_start_time=session.live_start_time,
        state=session.state,
        started_at=session.started_at,
        ended_at=session.ended_at,
        title=session.title,
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
        upload_job=(None if upload_job is None else _upload_job_response(upload_job)),
        parts=[_part_response(part) for part in session.parts],
    )


router = APIRouter(prefix='/recording-sessions', tags=['recording-sessions'])


@router.get('', response_model=RecordingSessionsResponse)
async def list_recording_sessions(
    limit: int = Query(50, ge=1, le=200),
    _subject: str = Depends(authenticated_manager_subject),
    recording_journal: RecordingJournalBridge = Depends(get_recording_journal),
) -> RecordingSessionsResponse:
    sessions = await recording_journal.list_sessions(limit=limit)
    upload_jobs = await recording_journal.upload_jobs_for_sessions(
        [session.id for session in sessions]
    )
    return RecordingSessionsResponse(
        degraded_reason=recording_journal.degraded_reason,
        sessions=[
            _session_response(session, upload_jobs.get(session.id))
            for session in sessions
        ],
    )


@router.post(
    '/danmaku-items/{item_id}/decision', status_code=status.HTTP_204_NO_CONTENT
)
async def decide_unknown_danmaku(
    item_id: int,
    command: DanmakuDecisionRequest,
    subject: str = Depends(authenticated_manager_subject),
    publisher: DanmakuPublisher = Depends(get_danmaku_publisher),
) -> Response:
    try:
        if command.action == 'assume_success':
            await publisher.assume_success(
                item_id, manager_subject=subject, reason=command.reason
            )
        else:
            await publisher.retry_accept_duplicate_risk(
                item_id, manager_subject=subject, reason=command.reason
            )
    except ValueError as error:
        code = (
            status.HTTP_404_NOT_FOUND
            if str(error).startswith('unknown danmaku item')
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=code, detail=str(error)) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
