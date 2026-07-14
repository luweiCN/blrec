from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, Field

from blrec.bili_upload.collections import (
    CollectionCatalogView,
    CollectionCreationView,
    CollectionManager,
    CollectionUnavailable,
    InvalidCollectionRequest,
)
from blrec.utils.string import camel_case

from .bili_accounts import authenticated_manager_subject

manager: Optional[CollectionManager] = None
unavailable_reason: Optional[str] = 'Bilibili collections are not enabled'


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class CollectionSectionResponse(ApiModel):
    id: int
    title: str


class CollectionResponse(ApiModel):
    id: int
    title: str
    description: str
    cover_url: str
    state: int
    reject_reason: str
    selectable: bool
    sections: List[CollectionSectionResponse]


class CollectionCatalogResponse(ApiModel):
    account_id: int
    collections: List[CollectionResponse]


class CollectionCreationResponse(ApiModel):
    account_id: int
    collection: CollectionResponse


class CollectionCreateRequest(ApiModel):
    account_mode: Literal['primary', 'fixed']
    account_id: Optional[int] = None
    title: str = Field(..., min_length=1, max_length=100)
    description: str = Field('', max_length=2000)
    cover_asset_id: int = Field(..., gt=0)


def get_manager() -> CollectionManager:
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Bilibili collections are unavailable',
        )
    return manager


router = APIRouter(prefix='/bili-collections', tags=['bili-collections'])


@router.get('', response_model=CollectionCatalogResponse)
async def list_bili_collections(
    account_mode: Literal['primary', 'fixed'] = Query(..., alias='accountMode'),
    account_id: Optional[int] = Query(None, alias='accountId'),
    _subject: str = Depends(authenticated_manager_subject),
    collection_manager: CollectionManager = Depends(get_manager),
) -> CollectionCatalogView:
    try:
        return await collection_manager.list(account_mode, account_id)
    except InvalidCollectionRequest as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    except CollectionUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from None


@router.post(
    '', response_model=CollectionCreationResponse, status_code=status.HTTP_201_CREATED
)
async def create_bili_collection(
    payload: CollectionCreateRequest,
    _subject: str = Depends(authenticated_manager_subject),
    collection_manager: CollectionManager = Depends(get_manager),
) -> CollectionCreationView:
    try:
        return await collection_manager.create(
            payload.account_mode,
            payload.account_id,
            title=payload.title,
            description=payload.description,
            cover_asset_id=payload.cover_asset_id,
        )
    except InvalidCollectionRequest as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    except CollectionUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from None
