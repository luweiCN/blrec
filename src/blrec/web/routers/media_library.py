from __future__ import annotations

from typing import Awaitable, Callable, List, Literal, Optional, Sequence

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, Field

from blrec.bili_upload.media_library import (
    ImportPartRequest,
    MediaLibrary,
    MediaLibraryConflict,
    MediaLibraryItem,
    MediaLibraryNotFound,
    MediaLibraryPart,
    SubmissionHistoryEntry,
)
from blrec.utils.string import camel_case

from .bili_accounts import authenticated_manager_subject

library: Optional[MediaLibrary] = None
item_deleter: Optional[Callable[..., Awaitable[int]]] = None
unavailable_reason: Optional[str] = 'Media library is not ready'


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class ImportPartInput(ApiModel):
    filename: str = Field(..., min_length=1, max_length=512)
    size_bytes: int = Field(..., gt=0)


class CreateImportRequest(ApiModel):
    kind: Literal['broadcast', 'clip']
    display_name: str = Field(..., min_length=1, max_length=200)
    note: str = Field('', max_length=2000)
    tags: List[str] = Field(default_factory=list, max_items=20)
    room_id: int = Field(0, ge=0)
    anchor_name: str = Field('', max_length=200)
    parts: List[ImportPartInput] = Field(..., min_items=1, max_items=100)


class UpdateMediaLibraryItemRequest(ApiModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=200)
    note: Optional[str] = Field(None, max_length=2000)
    tags: Optional[List[str]] = Field(None, max_items=20)


class MediaLibraryPartResponse(ApiModel):
    item_id: int
    part_index: int
    recording_part_id: Optional[int]
    original_filename: str
    expected_size: int
    received_size: int
    state: str
    error: Optional[str]
    duration_seconds: Optional[int]


class SubmissionHistoryResponse(ApiModel):
    aid: int
    bvid: str
    state: str
    account_id: int
    account_name: str
    occurred_at: int
    current: bool


class MediaLibraryItemResponse(ApiModel):
    id: int
    session_id: int
    kind: str
    origin: str
    display_name: str
    note: str
    state: str
    error: Optional[str]
    created_at: int
    updated_at: int
    room_id: int
    source_title: str
    anchor_name: str
    started_at: int
    tags: List[str]
    parts: List[MediaLibraryPartResponse]
    submissions: List[SubmissionHistoryResponse]


class MediaLibraryListResponse(ApiModel):
    total: int
    items: List[MediaLibraryItemResponse]


class DeleteMediaLibraryItemResponse(ApiModel):
    state: Literal['requested']
    generation: int


def get_library() -> MediaLibrary:
    if library is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Media library is unavailable',
        )
    return library


def get_item_deleter() -> Callable[..., Awaitable[int]]:
    if item_deleter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Media library deletion is unavailable',
        )
    return item_deleter


def _part_response(part: MediaLibraryPart) -> MediaLibraryPartResponse:
    return MediaLibraryPartResponse(
        item_id=part.item_id,
        part_index=part.part_index,
        recording_part_id=part.recording_part_id,
        original_filename=part.original_filename,
        expected_size=part.expected_size,
        received_size=part.received_size,
        state=part.state,
        error=part.error,
        duration_seconds=part.duration_seconds,
    )


def _submission_response(entry: SubmissionHistoryEntry) -> SubmissionHistoryResponse:
    return SubmissionHistoryResponse(
        aid=entry.aid,
        bvid=entry.bvid,
        state=entry.state,
        account_id=entry.account_id,
        account_name=entry.account_name,
        occurred_at=entry.occurred_at,
        current=entry.current,
    )


async def _item_response(
    media_library: MediaLibrary,
    item: MediaLibraryItem,
    history: Optional[Sequence[SubmissionHistoryEntry]] = None,
) -> MediaLibraryItemResponse:
    resolved_history = (
        await media_library.submission_history(item.id) if history is None else history
    )
    return MediaLibraryItemResponse(
        id=item.id,
        session_id=item.session_id,
        kind=item.kind,
        origin=item.origin,
        display_name=item.display_name,
        note=item.note,
        state=item.state,
        error=item.error,
        created_at=item.created_at,
        updated_at=item.updated_at,
        room_id=item.room_id,
        source_title=item.title,
        anchor_name=item.anchor_name,
        started_at=item.started_at,
        tags=list(item.tags),
        parts=[_part_response(part) for part in item.parts],
        submissions=[_submission_response(entry) for entry in resolved_history],
    )


def _raise_library_error(error: ValueError) -> None:
    if isinstance(error, MediaLibraryNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


router = APIRouter(prefix='/media-library', tags=['media-library'])


@router.get('', response_model=MediaLibraryListResponse)
async def list_media_library(
    kind: Optional[Literal['broadcast', 'clip']] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    query: str = Query('', alias='q', max_length=100),
    _subject: str = Depends(authenticated_manager_subject),
    media_library: MediaLibrary = Depends(get_library),
) -> MediaLibraryListResponse:
    total, items = await media_library.list_items(
        kind=kind, limit=limit, offset=offset, query=query
    )
    histories = await media_library.submission_histories(
        tuple(item.id for item in items)
    )
    return MediaLibraryListResponse(
        total=total,
        items=[
            await _item_response(media_library, item, histories.get(item.id, ()))
            for item in items
        ],
    )


@router.get('/{item_id}', response_model=MediaLibraryItemResponse)
async def get_media_library_item(
    item_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    media_library: MediaLibrary = Depends(get_library),
) -> MediaLibraryItemResponse:
    try:
        item = await media_library.get_item(item_id)
        return await _item_response(media_library, item)
    except (MediaLibraryConflict, MediaLibraryNotFound) as error:
        _raise_library_error(error)
        raise AssertionError('unreachable')


@router.post(
    '/favorites/{session_id}',
    response_model=MediaLibraryItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def favorite_recording_session(
    session_id: int,
    manager_subject: str = Depends(authenticated_manager_subject),
    media_library: MediaLibrary = Depends(get_library),
) -> MediaLibraryItemResponse:
    try:
        item = await media_library.favorite(session_id, manager_subject=manager_subject)
        return await _item_response(media_library, item)
    except (MediaLibraryConflict, MediaLibraryNotFound) as error:
        _raise_library_error(error)
        raise AssertionError('unreachable')


@router.post(
    '/imports',
    response_model=MediaLibraryItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_media_import(
    command: CreateImportRequest,
    manager_subject: str = Depends(authenticated_manager_subject),
    media_library: MediaLibrary = Depends(get_library),
) -> MediaLibraryItemResponse:
    try:
        item = await media_library.create_import(
            kind=command.kind,
            display_name=command.display_name,
            note=command.note,
            tags=command.tags,
            room_id=command.room_id,
            anchor_name=command.anchor_name,
            parts=tuple(
                ImportPartRequest(part.filename, part.size_bytes)
                for part in command.parts
            ),
            manager_subject=manager_subject,
        )
        return await _item_response(media_library, item)
    except (MediaLibraryConflict, MediaLibraryNotFound) as error:
        _raise_library_error(error)
        raise AssertionError('unreachable')


@router.put(
    '/{item_id}/parts/{part_index}/content', response_model=MediaLibraryPartResponse
)
async def upload_media_import_part(
    item_id: int,
    part_index: int,
    request: Request,
    _subject: str = Depends(authenticated_manager_subject),
    media_library: MediaLibrary = Depends(get_library),
) -> MediaLibraryPartResponse:
    try:
        part = await media_library.upload_part(item_id, part_index, request.stream())
        return _part_response(part)
    except (MediaLibraryConflict, MediaLibraryNotFound) as error:
        _raise_library_error(error)
        raise AssertionError('unreachable')


@router.post('/{item_id}/complete', response_model=MediaLibraryItemResponse)
async def complete_media_import(
    item_id: int,
    manager_subject: str = Depends(authenticated_manager_subject),
    media_library: MediaLibrary = Depends(get_library),
) -> MediaLibraryItemResponse:
    try:
        item = await media_library.complete_import(
            item_id, manager_subject=manager_subject
        )
        return await _item_response(media_library, item)
    except (MediaLibraryConflict, MediaLibraryNotFound) as error:
        _raise_library_error(error)
        raise AssertionError('unreachable')


@router.patch('/{item_id}', response_model=MediaLibraryItemResponse)
async def update_media_library_item(
    item_id: int,
    command: UpdateMediaLibraryItemRequest,
    manager_subject: str = Depends(authenticated_manager_subject),
    media_library: MediaLibrary = Depends(get_library),
) -> MediaLibraryItemResponse:
    try:
        item = await media_library.update_item(
            item_id,
            manager_subject=manager_subject,
            display_name=command.display_name,
            note=command.note,
            tags=command.tags,
        )
        return await _item_response(media_library, item)
    except (MediaLibraryConflict, MediaLibraryNotFound) as error:
        _raise_library_error(error)
        raise AssertionError('unreachable')


@router.delete(
    '/{item_id}',
    response_model=DeleteMediaLibraryItemResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_media_library_item(
    item_id: int,
    manager_subject: str = Depends(authenticated_manager_subject),
    delete_item: Callable[..., Awaitable[int]] = Depends(get_item_deleter),
) -> DeleteMediaLibraryItemResponse:
    try:
        generation = await delete_item(item_id, manager_subject=manager_subject)
    except (MediaLibraryConflict, MediaLibraryNotFound) as error:
        _raise_library_error(error)
        raise AssertionError('unreachable')
    return DeleteMediaLibraryItemResponse(state='requested', generation=generation)
