from __future__ import annotations

import time
from pathlib import Path
from typing import Awaitable, Callable, List, Literal, Mapping, Optional

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, Field, validator
from starlette.responses import Response, StreamingResponse

from blrec.bili_upload.highlight_cut import ClipInspection, HighlightCutError
from blrec.bili_upload.highlight_worker import HighlightWorker
from blrec.bili_upload.highlights import (
    HighlightClip,
    HighlightConfirmationRequired,
    HighlightMarker,
    HighlightRangeUnavailable,
    HighlightService,
    HighlightTimeline,
)
from blrec.bili_upload.task_actions import UploadTaskActionRejected
from blrec.bili_upload.upload import InvalidUploadPolicy
from blrec.utils.string import camel_case

from .. import security
from .bili_accounts import authenticated_manager_subject
from .recording_sessions import RangeNotSatisfiable, file_chunks, parse_byte_range

service: Optional[HighlightService] = None
worker: Optional[HighlightWorker] = None
upload_task_creator: Optional[Callable[..., Awaitable[int]]] = None
active_durations_provider: Optional[Callable[[int], Awaitable[Mapping[int, int]]]] = (
    None
)
unavailable_reason: Optional[str] = 'Highlight editing is not ready'
_MEDIA_ACCESS_TTL_SECONDS = 2 * 60 * 60


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class CreateMarkerRequest(ApiModel):
    room_id: int = Field(..., gt=0)
    observed_at_ms: int = Field(..., gt=0)
    player_delay_ms: int = Field(0, ge=0, le=300_000)
    current_time_ms: Optional[int] = Field(None, ge=0, le=604_800_000)
    seekable_end_ms: Optional[int] = Field(None, ge=0, le=604_800_000)
    raw_delay_ms: int = Field(0, ge=0, le=86_400_000)
    baseline_delay_ms: int = Field(0, ge=0, le=86_400_000)
    effective_rewind_ms: Optional[int] = Field(None, ge=0, le=86_400_000)
    name: str = Field('', max_length=200)
    title: str = Field('', max_length=200)
    anchor_name: str = Field('', max_length=100)
    source: Literal['web', 'browser_extension'] = 'web'


class UpdateMarkerRequest(ApiModel):
    name: str = Field(..., min_length=1, max_length=200)
    note: str = Field('', max_length=1000)

    @validator('name')
    def name_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError('name must not be blank')
        return normalized


class MarkerResponse(ApiModel):
    id: int
    room_id: int
    observed_at_ms: int
    player_delay_ms: int
    content_at_ms: int
    title: str
    anchor_name: str
    name: str
    note: str
    source: str
    created_at: int
    updated_at: int
    recording_part_id: Optional[int]
    part_anchor_at_ms: Optional[int]
    current_time_ms: Optional[int]
    seekable_end_ms: Optional[int]
    raw_delay_ms: int
    baseline_delay_ms: int
    effective_rewind_ms: int


class TimelinePartResponse(ApiModel):
    part_id: int
    part_index: int
    timeline_start_ms: int
    duration_ms: int
    stable_end_ms: int
    recording: bool
    media_kind: Literal['flv', 'native']


class MappedMarkerResponse(ApiModel):
    marker: MarkerResponse
    part_id: int
    local_offset_ms: int
    timeline_offset_ms: int


class TimelineResponse(ApiModel):
    session_id: int
    room_id: int
    duration_ms: int
    stable_end_ms: int
    parts: List[TimelinePartResponse]
    markers: List[MappedMarkerResponse]


class InspectClipRequest(ApiModel):
    start_ms: int = Field(..., ge=0)
    end_ms: int = Field(..., gt=0)


class CreateClipRequest(InspectClipRequest):
    marker_id: Optional[int] = Field(None, gt=0)
    name: str = Field(..., min_length=1, max_length=200)
    confirm_keyframe: bool = False


class InspectedSourceResponse(ApiModel):
    part_id: int
    actual_start_ms: int
    actual_end_ms: int
    output_offset_ms: int


class ClipInspectionResponse(ApiModel):
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: int
    actual_end_ms: int
    extra_lead_ms: int
    confirmation_required: bool
    compatible: bool = True
    sources: List[InspectedSourceResponse]


class ClipSourceResponse(ApiModel):
    part_id: int
    ordinal: int
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: Optional[int]
    actual_end_ms: Optional[int]


class ClipResponse(ApiModel):
    id: int
    marker_id: Optional[int]
    room_id: int
    source_session_id: Optional[int]
    upload_session_id: Optional[int]
    name: str
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: Optional[int]
    actual_end_ms: Optional[int]
    output_video_path: Optional[str]
    output_xml_path: Optional[str]
    state: str
    confirmation_required: bool
    confirmed: bool
    error_message: Optional[str]
    attempt: int
    created_at: int
    updated_at: int
    sources: List[ClipSourceResponse]
    upload_job_id: Optional[int]
    upload_state: Optional[str]
    upload_percent: Optional[float]
    upload_bvid: Optional[str]


class UploadTaskResponse(ApiModel):
    job_id: int


class ClipMediaAccessResponse(ApiModel):
    token: str
    expires_at: int
    file_size_bytes: int


def get_service() -> HighlightService:
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Highlight editing is unavailable',
        )
    return service


def get_upload_task_creator() -> Callable[..., Awaitable[int]]:
    if upload_task_creator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Highlight upload is unavailable',
        )
    return upload_task_creator


async def _active_durations(session_id: int) -> Mapping[int, int]:
    if active_durations_provider is None:
        return {}
    return await active_durations_provider(session_id)


async def authenticated_clip_media_subject(
    request: Request,
    clip_id: int,
    media_token: Optional[str] = Query(None),
    media_expires: Optional[int] = Query(None),
    x_api_key: Optional[str] = Header(None),
) -> str:
    if media_token is not None or media_expires is not None:
        if (
            media_token is not None
            and media_expires is not None
            and security.valid_media_access(-clip_id, media_expires, media_token)
        ):
            return 'highlight-media-access'
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail='播放凭据无效或已过期'
        )
    return await authenticated_manager_subject(request, x_api_key)


async def _clip_video_path(clip_id: int, highlight_service: HighlightService) -> Path:
    try:
        return await highlight_service.clip_video_path(clip_id)
    except ValueError as error:
        message = str(error)
        code = (
            status.HTTP_404_NOT_FOUND
            if 'unknown highlight clip' in message
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=code, detail=message) from None


def _marker_response(value: HighlightMarker) -> MarkerResponse:
    return MarkerResponse(**value.__dict__)


def _timeline_response(value: HighlightTimeline) -> TimelineResponse:
    return TimelineResponse(
        session_id=value.session_id,
        room_id=value.room_id,
        duration_ms=value.duration_ms,
        stable_end_ms=value.stable_end_ms,
        parts=[
            TimelinePartResponse(
                part_id=part.part_id,
                part_index=part.part_index,
                timeline_start_ms=part.timeline_start_ms,
                duration_ms=part.duration_ms,
                stable_end_ms=part.stable_end_ms,
                recording=part.recording,
                media_kind=('flv' if part.path.lower().endswith('.flv') else 'native'),
            )
            for part in value.parts
        ],
        markers=[
            MappedMarkerResponse(
                marker=_marker_response(item.marker),
                part_id=item.part_id,
                local_offset_ms=item.local_offset_ms,
                timeline_offset_ms=item.timeline_offset_ms,
            )
            for item in value.markers
        ],
    )


def _inspection_response(value: ClipInspection) -> ClipInspectionResponse:
    return ClipInspectionResponse(
        requested_start_ms=value.requested_start_ms,
        requested_end_ms=value.requested_end_ms,
        actual_start_ms=value.actual_start_ms,
        actual_end_ms=value.actual_end_ms,
        extra_lead_ms=value.extra_lead_ms,
        confirmation_required=value.confirmation_required,
        compatible=True,
        sources=[
            InspectedSourceResponse(
                part_id=source.part_id,
                actual_start_ms=source.actual_start_ms,
                actual_end_ms=source.actual_end_ms,
                output_offset_ms=source.output_offset_ms,
            )
            for source in value.sources
        ],
    )


def _clip_response(value: HighlightClip) -> ClipResponse:
    return ClipResponse(
        id=value.id,
        marker_id=value.marker_id,
        room_id=value.room_id,
        source_session_id=value.source_session_id,
        upload_session_id=value.upload_session_id,
        name=value.name,
        requested_start_ms=value.requested_start_ms,
        requested_end_ms=value.requested_end_ms,
        actual_start_ms=value.actual_start_ms,
        actual_end_ms=value.actual_end_ms,
        output_video_path=value.output_video_path,
        output_xml_path=value.output_xml_path,
        state=value.state,
        confirmation_required=value.confirmation_required,
        confirmed=value.confirmed,
        error_message=value.error_message,
        attempt=value.attempt,
        created_at=value.created_at,
        updated_at=value.updated_at,
        sources=[ClipSourceResponse(**source.__dict__) for source in value.sources],
        upload_job_id=value.upload_job_id,
        upload_state=value.upload_state,
        upload_percent=value.upload_percent,
        upload_bvid=value.upload_bvid,
    )


def _not_found(error: ValueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def _clip_conflict(error: Exception) -> HTTPException:
    if isinstance(error, HighlightConfirmationRequired):
        value = error.inspection
        detail: object = {
            'code': 'keyframe_confirmation_required',
            'message': str(error),
            'extraLeadMs': value.extra_lead_ms,
            'actualStartMs': value.actual_start_ms,
            'actualEndMs': value.actual_end_ms,
        }
    else:
        detail = str(error)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


router = APIRouter(prefix='/highlights', tags=['highlights'])


@router.post('', response_model=MarkerResponse, status_code=status.HTTP_201_CREATED)
async def create_marker(
    payload: CreateMarkerRequest,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> MarkerResponse:
    value = await highlight_service.create_marker(**payload.dict())
    return _marker_response(value)


@router.patch('/{marker_id}', response_model=MarkerResponse)
async def update_marker(
    marker_id: int,
    payload: UpdateMarkerRequest,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> MarkerResponse:
    try:
        value = await highlight_service.update_marker(
            marker_id, payload.name, payload.note
        )
    except ValueError as error:
        raise _not_found(error) from None
    return _marker_response(value)


@router.delete('/{marker_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_marker(
    marker_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> Response:
    try:
        await highlight_service.delete_marker(marker_id)
    except ValueError as error:
        raise _not_found(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get('/sessions/{session_id}/timeline', response_model=TimelineResponse)
async def get_timeline(
    session_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> TimelineResponse:
    try:
        value = await highlight_service.timeline(
            session_id, await _active_durations(session_id)
        )
    except ValueError as error:
        raise _not_found(error) from None
    return _timeline_response(value)


@router.post(
    '/sessions/{session_id}/clips/inspect', response_model=ClipInspectionResponse
)
async def inspect_clip(
    session_id: int,
    payload: InspectClipRequest,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> ClipInspectionResponse:
    try:
        value = await highlight_service.inspect_clip(
            session_id=session_id,
            requested_start_ms=payload.start_ms,
            requested_end_ms=payload.end_ms,
            active_durations_ms=await _active_durations(session_id),
        )
    except (HighlightRangeUnavailable, HighlightCutError) as error:
        raise _clip_conflict(error) from None
    except ValueError as error:
        raise _not_found(error) from None
    return _inspection_response(value)


@router.post(
    '/sessions/{session_id}/clips',
    response_model=ClipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_clip(
    session_id: int,
    payload: CreateClipRequest,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> ClipResponse:
    try:
        value = await highlight_service.create_clip(
            session_id=session_id,
            marker_id=payload.marker_id,
            name=payload.name,
            requested_start_ms=payload.start_ms,
            requested_end_ms=payload.end_ms,
            confirm_keyframe=payload.confirm_keyframe,
            active_durations_ms=await _active_durations(session_id),
        )
    except (
        HighlightRangeUnavailable,
        HighlightConfirmationRequired,
        HighlightCutError,
    ) as error:
        raise _clip_conflict(error) from None
    return _clip_response(value)


@router.get('/sessions/{session_id}/clips', response_model=List[ClipResponse])
async def list_clips(
    session_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> List[ClipResponse]:
    try:
        values = await highlight_service.list_clips(session_id)
    except ValueError as error:
        raise _not_found(error) from None
    return [_clip_response(value) for value in values]


@router.get('/clips/{clip_id}', response_model=ClipResponse)
async def get_clip(
    clip_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> ClipResponse:
    try:
        value = await highlight_service.get_clip(clip_id)
    except ValueError as error:
        raise _not_found(error) from None
    return _clip_response(value)


@router.post('/clips/{clip_id}/media-access', response_model=ClipMediaAccessResponse)
async def create_clip_media_access(
    clip_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> ClipMediaAccessResponse:
    path = await _clip_video_path(clip_id, highlight_service)
    expires_at = int(time.time()) + _MEDIA_ACCESS_TTL_SECONDS
    return ClipMediaAccessResponse(
        token=security.media_access_token(-clip_id, expires_at),
        expires_at=expires_at,
        file_size_bytes=path.stat().st_size,
    )


@router.get('/clips/{clip_id}/media')
async def stream_clip_media(
    clip_id: int,
    range_header: Optional[str] = Header(None, alias='Range'),
    _subject: str = Depends(authenticated_clip_media_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> StreamingResponse:
    path = await _clip_video_path(clip_id, highlight_service)
    try:
        size = path.stat().st_size
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='高光片段文件不可用'
        ) from None
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
    length = end - start + 1
    headers = {
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-store',
        'Content-Length': str(length),
    }
    if response_status == status.HTTP_206_PARTIAL_CONTENT:
        headers['Content-Range'] = 'bytes {}-{}/{}'.format(start, end, size)
    try:
        file = open(path, 'rb')
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='高光片段文件不可用'
        ) from None
    return StreamingResponse(
        file_chunks(file, start=start, length=length),
        status_code=response_status,
        media_type='video/mp4',
        headers=headers,
    )


@router.delete('/clips/{clip_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_clip(
    clip_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> Response:
    try:
        await highlight_service.delete_clip(clip_id)
    except ValueError as error:
        if 'upload task' in str(error):
            raise _clip_conflict(error) from None
        raise _not_found(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    '/clips/{clip_id}/upload-task',
    response_model=UploadTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_upload_task(
    clip_id: int,
    subject: str = Depends(authenticated_manager_subject),
    creator: Callable[..., Awaitable[int]] = Depends(get_upload_task_creator),
) -> UploadTaskResponse:
    try:
        job_id = await creator(clip_id, manager_subject=subject)
    except (ValueError, InvalidUploadPolicy, UploadTaskActionRejected) as error:
        raise _clip_conflict(error) from None
    return UploadTaskResponse(job_id=job_id)
