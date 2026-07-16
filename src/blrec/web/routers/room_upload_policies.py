from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, Field

from blrec.bili_upload.categories import (
    InvalidUploadCategoryRequest,
    UploadCategoryCatalog,
    UploadCategoryCatalogView,
    UploadCategoryUnavailable,
)
from blrec.bili_upload.policies import (
    InvalidRoomUploadPolicy,
    RoomUploadPolicyCommand,
    RoomUploadPolicyManager,
    RoomUploadPolicyNotFound,
    RoomUploadPolicyView,
)
from blrec.logging.audit import audit
from blrec.utils.string import camel_case

from .bili_accounts import authenticated_manager_subject

manager: Optional[RoomUploadPolicyManager] = None
category_catalog: Optional[UploadCategoryCatalog] = None
unavailable_reason: Optional[str] = 'Room upload policies are not ready'


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class RoomUploadPolicyRequest(ApiModel):
    account_mode: Literal['primary', 'fixed']
    account_id: Optional[int] = None
    enabled: bool
    title_template: str
    description_template: str
    part_title_template: str
    dynamic_template: str
    tid: int = Field(..., gt=0)
    tags: str
    creation_statement_id: int
    original_authorization: bool
    source: str
    is_only_self: bool
    publish_dynamic: bool
    up_selection_reply: bool
    up_close_reply: bool
    up_close_danmu: bool
    auto_comment: bool
    danmaku_backfill: bool
    filters: Dict[str, Any] = Field(default_factory=dict)
    collection_season_id: Optional[int] = Field(None, gt=0)
    collection_section_id: Optional[int] = Field(None, gt=0)
    cover_mode: Literal['live', 'custom'] = 'live'
    cover_asset_id: Optional[int] = Field(None, gt=0)
    publish_delay_seconds: int = Field(0, ge=0)
    retention_mode: Literal[
        'never', 'upload_completed', 'submitted', 'approved', 'capacity'
    ] = 'submitted'
    retention_days: int = Field(5, ge=0, le=3650)

    def to_command(self) -> RoomUploadPolicyCommand:
        return RoomUploadPolicyCommand(
            account_mode=self.account_mode,
            account_id=self.account_id,
            enabled=self.enabled,
            title_template=self.title_template,
            description_template=self.description_template,
            part_title_template=self.part_title_template,
            dynamic_template=self.dynamic_template,
            tid=self.tid,
            tags=self.tags,
            creation_statement_id=self.creation_statement_id,
            original_authorization=self.original_authorization,
            source=self.source,
            is_only_self=self.is_only_self,
            publish_dynamic=self.publish_dynamic,
            up_selection_reply=self.up_selection_reply,
            up_close_reply=self.up_close_reply,
            up_close_danmu=self.up_close_danmu,
            auto_comment=self.auto_comment,
            danmaku_backfill=self.danmaku_backfill,
            filters=self.filters,
            collection_season_id=self.collection_season_id,
            collection_section_id=self.collection_section_id,
            cover_mode=self.cover_mode,
            cover_asset_id=self.cover_asset_id,
            publish_delay_seconds=self.publish_delay_seconds,
            retention_mode=self.retention_mode,
            retention_days=self.retention_days,
        )


class RoomUploadPolicyResponse(ApiModel):
    room_id: int
    account_mode: str
    account_id: Optional[int]
    resolved_account_id: Optional[int]
    resolved_account_name: Optional[str]
    enabled: bool
    title_template: str
    description_template: str
    part_title_template: str
    dynamic_template: str
    tid: int
    tags: str
    creation_statement_id: int
    original_authorization: bool
    source: str
    is_only_self: bool
    publish_dynamic: bool
    up_selection_reply: bool
    up_close_reply: bool
    up_close_danmu: bool
    auto_comment: bool
    danmaku_backfill: bool
    filters: Dict[str, Any]
    blocked_reason: Optional[str]
    created_at: int
    updated_at: int
    collection_season_id: Optional[int]
    collection_section_id: Optional[int]
    cover_mode: str
    cover_asset_id: Optional[int]
    publish_delay_seconds: int
    retention_mode: str
    retention_days: int


class UploadCategoryNodeResponse(ApiModel):
    id: int
    name: str
    description: str
    children: List['UploadCategoryNodeResponse'] = Field(default_factory=list)


class UploadCreationStatementResponse(ApiModel):
    id: int
    content: str


class UploadCategoryCatalogResponse(ApiModel):
    account_id: int
    credential_version: int
    fetched_at: int
    stale: bool
    categories: List[UploadCategoryNodeResponse]
    creation_statements: List[UploadCreationStatementResponse]
    creation_statement_tip: str


def get_policy_manager() -> RoomUploadPolicyManager:
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Room upload policies are unavailable',
        )
    return manager


def get_category_catalog() -> UploadCategoryCatalog:
    if category_catalog is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Upload categories are unavailable',
        )
    return category_catalog


router = APIRouter(prefix='/room-upload-policies', tags=['room-upload-policies'])


@router.get('', response_model=List[RoomUploadPolicyResponse])
async def list_room_upload_policies(
    _subject: str = Depends(authenticated_manager_subject),
    policy_manager: RoomUploadPolicyManager = Depends(get_policy_manager),
) -> List[RoomUploadPolicyView]:
    return await policy_manager.list()


@router.get('/categories', response_model=UploadCategoryCatalogResponse)
async def list_upload_categories(
    account_mode: Literal['primary', 'fixed'] = Query(..., alias='accountMode'),
    account_id: Optional[int] = Query(None, alias='accountId'),
    refresh: bool = False,
    _subject: str = Depends(authenticated_manager_subject),
    catalog: UploadCategoryCatalog = Depends(get_category_catalog),
) -> UploadCategoryCatalogView:
    try:
        return await catalog.list(account_mode, account_id, force_refresh=refresh)
    except InvalidUploadCategoryRequest as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    except UploadCategoryUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from None


@router.get('/{room_id}', response_model=RoomUploadPolicyResponse)
async def get_room_upload_policy(
    room_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    policy_manager: RoomUploadPolicyManager = Depends(get_policy_manager),
) -> RoomUploadPolicyView:
    try:
        return await policy_manager.get(room_id)
    except RoomUploadPolicyNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Room upload policy not found'
        ) from None


@router.put('/{room_id}', response_model=RoomUploadPolicyResponse)
async def upsert_room_upload_policy(
    room_id: int,
    payload: RoomUploadPolicyRequest,
    _subject: str = Depends(authenticated_manager_subject),
    policy_manager: RoomUploadPolicyManager = Depends(get_policy_manager),
    catalog: UploadCategoryCatalog = Depends(get_category_catalog),
) -> RoomUploadPolicyView:
    try:
        category_view = await catalog.list(payload.account_mode, payload.account_id)
        if not any(
            child.id == payload.tid
            for parent in category_view.categories
            for child in parent.children
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail='请选择有效的二级投稿分区'
            )
        if not any(
            statement.id == payload.creation_statement_id
            for statement in category_view.creation_statements
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='请选择当前账号支持的创作声明',
            )
        result = await policy_manager.upsert(room_id, payload.to_command())
    except InvalidUploadCategoryRequest as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    except UploadCategoryUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from None
    except InvalidRoomUploadPolicy as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    audit(
        'room_upload_policy_updated',
        room_id=room_id,
        account_mode=payload.account_mode,
        account_id=payload.account_id,
        enabled=payload.enabled,
        tid=payload.tid,
        is_only_self=payload.is_only_self,
        publish_dynamic=payload.publish_dynamic,
        auto_comment=payload.auto_comment,
        danmaku_backfill=payload.danmaku_backfill,
        collection_enabled=payload.collection_section_id is not None,
        cover_mode=payload.cover_mode,
        publish_delay_seconds=payload.publish_delay_seconds,
    )
    return result


@router.delete('/{room_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_room_upload_policy(
    room_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    policy_manager: RoomUploadPolicyManager = Depends(get_policy_manager),
) -> Response:
    try:
        await policy_manager.delete(room_id)
    except RoomUploadPolicyNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Room upload policy not found'
        ) from None
    audit('room_upload_policy_deleted', room_id=room_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
