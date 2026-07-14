import hashlib
import hmac
import re
import time
from typing import BinaryIO, Iterator, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, Field, validator
from starlette.responses import Response, StreamingResponse

from blrec.bili_upload.danmaku_publish import DanmakuPublisher
from blrec.bili_upload.journal import (
    DanmakuItemProgress,
    RecordingJournalBridge,
    RecordingPart,
    RecordingSession,
    UploadJobProgress,
    UploadPartProgress,
)
from blrec.bili_upload.recording_content import (
    DanmakuPage,
    RecordingContentInvalid,
    RecordingContentNotFound,
    RecordingContentReader,
    RecordingContentUnavailable,
)
from blrec.bili_upload.task_actions import (
    UploadTaskActionManager,
    UploadTaskActionRejected,
)
from blrec.utils.string import camel_case

from .. import security
from .bili_accounts import authenticated_manager_subject

journal: Optional[RecordingJournalBridge] = None
danmaku_publisher: Optional[DanmakuPublisher] = None
content_reader: Optional[RecordingContentReader] = None
task_actions: Optional[UploadTaskActionManager] = None
unavailable_reason: Optional[str] = 'Recording journal is not enabled'

_BYTE_RANGE = re.compile(r'bytes=(\d*)-(\d*)')
_MEDIA_ACCESS_TTL_SECONDS = 2 * 60 * 60


class RangeNotSatisfiable(ValueError):
    pass


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
    transcode_state: str
    transcode_fail_code: Optional[int]
    transcode_fail_desc: Optional[str]


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
    repair_state: str
    repair_message: Optional[str]
    repair_error: Optional[str]
    can_retry: bool
    can_repair: bool
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


class UploadJobActionRequest(ApiModel):
    action: Literal['retry_failed', 'repair_transcode']
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
    total: int
    sessions: List[RecordingSessionResponse]


class DanmakuLineResponse(ApiModel):
    index: int
    progress_ms: int
    mode: int
    font_size: int
    color: int
    content: str


class DanmakuPageResponse(ApiModel):
    items: List[DanmakuLineResponse]
    next_cursor: Optional[int]


class MediaAccessResponse(ApiModel):
    token: str
    expires_at: int


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


def parse_byte_range(value: str, size: int) -> Tuple[int, int]:
    if size <= 0 or ',' in value:
        raise RangeNotSatisfiable()
    match = _BYTE_RANGE.fullmatch(value.strip())
    if match is None:
        raise RangeNotSatisfiable()
    first, last = match.groups()
    if not first:
        if not last:
            raise RangeNotSatisfiable()
        suffix = int(last)
        if suffix <= 0:
            raise RangeNotSatisfiable()
        return max(0, size - suffix), size - 1
    start = int(first)
    end = size - 1 if not last else min(int(last), size - 1)
    if start >= size or end < start:
        raise RangeNotSatisfiable()
    return start, end


def _file_chunks(
    file: BinaryIO, *, start: int, length: int, chunk_size: int = 64 * 1024
) -> Iterator[bytes]:
    try:
        file.seek(start)
        remaining = length
        while remaining > 0:
            chunk = file.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        file.close()


def _content_error(error: RuntimeError) -> HTTPException:
    if isinstance(error, RecordingContentNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


def _media_access_token(part_id: int, expires_at: int) -> str:
    value = '{}:{}'.format(int(part_id), int(expires_at)).encode('ascii')
    return hmac.new(security.api_key.encode('utf8'), value, hashlib.sha256).hexdigest()


def _valid_media_access(part_id: int, expires_at: int, token: str) -> bool:
    if not security.api_key or expires_at < int(time.time()):
        return False
    expected = _media_access_token(part_id, expires_at)
    return hmac.compare_digest(token, expected)


async def authenticated_media_subject(
    request: Request,
    part_id: int,
    media_token: Optional[str] = Query(None),
    media_expires: Optional[int] = Query(None),
    x_api_key: Optional[str] = Header(None),
) -> str:
    if media_token is not None or media_expires is not None:
        if (
            media_token is not None
            and media_expires is not None
            and _valid_media_access(part_id, media_expires, media_token)
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
        repair_state=job.repair_state,
        repair_message=job.repair_message,
        repair_error=job.repair_error,
        can_retry=job.can_retry,
        can_repair=job.can_repair,
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
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _subject: str = Depends(authenticated_manager_subject),
    recording_journal: RecordingJournalBridge = Depends(get_recording_journal),
) -> RecordingSessionsResponse:
    total = await recording_journal.count_sessions()
    sessions = await recording_journal.list_sessions(limit=limit, offset=offset)
    upload_jobs = await recording_journal.upload_jobs_for_sessions(
        [session.id for session in sessions]
    )
    return RecordingSessionsResponse(
        degraded_reason=recording_journal.degraded_reason,
        total=total,
        sessions=[
            _session_response(session, upload_jobs.get(session.id))
            for session in sessions
        ],
    )


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
            else:
                message = await actions.request_transcode_repair(
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
    return UploadJobActionResponse(results=results)


@router.post('/parts/{part_id}/media-access', response_model=MediaAccessResponse)
async def create_recording_media_access(
    part_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    reader: RecordingContentReader = Depends(get_content_reader),
) -> MediaAccessResponse:
    try:
        resource = await reader.media(part_id)
    except (RecordingContentNotFound, RecordingContentUnavailable) as error:
        raise _content_error(error) from None
    if resource.path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='该分 P 的本地视频不可用'
        )
    expires_at = int(time.time()) + _MEDIA_ACCESS_TTL_SECONDS
    return MediaAccessResponse(
        token=_media_access_token(part_id, expires_at), expires_at=expires_at
    )


@router.get('/parts/{part_id}/media')
async def stream_recording_media(
    part_id: int,
    range_header: Optional[str] = Header(None, alias='Range'),
    _subject: str = Depends(authenticated_media_subject),
    reader: RecordingContentReader = Depends(get_content_reader),
) -> StreamingResponse:
    try:
        resource = await reader.media(part_id)
    except (RecordingContentNotFound, RecordingContentUnavailable) as error:
        raise _content_error(error) from None
    if resource.path is None or resource.size is None or resource.content_type is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='该分 P 的本地视频不可用'
        )
    size = resource.size
    start, end = 0, size - 1
    response_status = status.HTTP_200_OK
    if range_header is not None:
        try:
            start, end = parse_byte_range(range_header, size)
        except RangeNotSatisfiable:
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                detail='请求的视频范围不可用',
                headers={'Content-Range': 'bytes */{}'.format(size)},
            ) from None
        response_status = status.HTTP_206_PARTIAL_CONTENT
    try:
        file = open(resource.path, 'rb')
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='该分 P 的本地视频不可用'
        ) from None
    length = max(0, end - start + 1)
    headers = {
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-store',
        'Content-Length': str(length),
    }
    if response_status == status.HTTP_206_PARTIAL_CONTENT:
        headers['Content-Range'] = 'bytes {}-{}/{}'.format(start, end, size)
    return StreamingResponse(
        _file_chunks(file, start=start, length=length),
        status_code=response_status,
        media_type=resource.content_type,
        headers=headers,
    )


@router.get('/parts/{part_id}/danmaku', response_model=DanmakuPageResponse)
async def list_recording_danmaku(
    part_id: int,
    cursor: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    _subject: str = Depends(authenticated_manager_subject),
    reader: RecordingContentReader = Depends(get_content_reader),
) -> DanmakuPageResponse:
    try:
        page: DanmakuPage = await reader.danmaku(part_id, cursor=cursor, limit=limit)
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
                content=item.content,
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
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
