from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel
from starlette.responses import FileResponse

from blrec.bili_upload.covers import (
    CoverAssetNotFound,
    CoverAssetView,
    CoverLibrary,
    CoverWorkSaturated,
    InvalidCover,
    StoredCoverUnavailable,
)
from blrec.utils.string import camel_case

from .bili_accounts import authenticated_manager_subject

library: Optional[CoverLibrary] = None
unavailable_reason: Optional[str] = 'Upload covers are not ready'


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class CoverAssetResponse(ApiModel):
    id: int
    filename: str
    mime_type: str
    width: int
    height: int
    byte_size: int
    created_at: int
    content_url: str


def get_library() -> CoverLibrary:
    if library is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Upload covers are unavailable',
        )
    return library


def response(asset: CoverAssetView) -> CoverAssetResponse:
    return CoverAssetResponse(
        id=asset.id,
        filename=asset.filename,
        mime_type=asset.mime_type,
        width=asset.width,
        height=asset.height,
        byte_size=asset.byte_size,
        created_at=asset.created_at,
        content_url='/api/v1/upload-covers/{}/content'.format(asset.id),
    )


router = APIRouter(prefix='/upload-covers', tags=['upload-covers'])


@router.get('', response_model=List[CoverAssetResponse])
async def list_upload_covers(
    _subject: str = Depends(authenticated_manager_subject),
    cover_library: CoverLibrary = Depends(get_library),
) -> List[CoverAssetResponse]:
    return [response(asset) for asset in await cover_library.list()]


@router.post('', response_model=CoverAssetResponse, status_code=status.HTTP_201_CREATED)
async def add_upload_cover(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=512),
    _subject: str = Depends(authenticated_manager_subject),
    cover_library: CoverLibrary = Depends(get_library),
) -> CoverAssetResponse:
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > CoverLibrary.MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail='Cover must not exceed 2 MiB',
            )
    try:
        asset = await cover_library.add(bytes(content), filename)
    except CoverWorkSaturated as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Cover processing is busy',
            headers={'Retry-After': str(error.retry_after)},
        ) from None
    except InvalidCover as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    except StoredCoverUnavailable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='Stored cover is unavailable'
        ) from None
    return response(asset)


@router.get('/{asset_id}/content', response_class=FileResponse)
async def read_upload_cover(
    asset_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    cover_library: CoverLibrary = Depends(get_library),
) -> FileResponse:
    try:
        opened = await cover_library.open(asset_id)
    except CoverAssetNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from None
    except StoredCoverUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    return FileResponse(
        opened.path,
        media_type=opened.view.mime_type,
        filename=opened.view.filename,
        content_disposition_type='inline',
        headers={'Cache-Control': 'private, max-age=3600'},
    )
