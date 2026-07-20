from __future__ import annotations

from dataclasses import fields
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, Field

from blrec.application import Application
from blrec.bili_upload.categories import (
    InvalidUploadCategoryRequest,
    UploadCategoryCatalog,
    UploadCategoryUnavailable,
)
from blrec.bili_upload.highlights import HighlightService
from blrec.bili_upload.policies import (
    InvalidRoomUploadPolicy,
    RoomUploadPolicyCommand,
    RoomUploadPolicyManager,
    RoomUploadPolicyNotFound,
    default_room_upload_policy,
)
from blrec.control.operations import ControlJournalClosed, ControlLaneSaturated
from blrec.logging.audit import audit
from blrec.task.models import RunningStatus
from blrec.utils.string import camel_case
from blrec.web import security
from blrec.web.auth_store import (
    AuthenticationFailed,
    AuthenticationRateLimited,
    ExtensionIdentity,
)

application: Optional[Application] = None
highlight_service: Optional[HighlightService] = None
policy_manager: Optional[RoomUploadPolicyManager] = None
category_catalog: Optional[UploadCategoryCatalog] = None
unavailable_reason: Optional[str] = 'Browser extension actions are not ready'


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class PairRequest(ApiModel):
    username: str = Field(..., min_length=1, max_length=64)


class PairResponse(ApiModel):
    token_id: int
    token: str


class RoomStatusResponse(ApiModel):
    collected: bool
    recording: bool


class CollectRequest(ApiModel):
    upload: bool = False


class CollectResponse(ApiModel):
    operation_id: str
    status: Literal['accepted', 'running', 'succeeded', 'failed']
    requested_room_id: int


class HighlightRequest(ApiModel):
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


class HighlightResponse(ApiModel):
    id: int
    name: str


def reset() -> None:
    global application, category_catalog, highlight_service, policy_manager
    application = None
    highlight_service = None
    policy_manager = None
    category_catalog = None


def _application() -> Application:
    if application is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Recording actions are unavailable',
        )
    return application


def _highlights() -> HighlightService:
    if highlight_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Highlight actions are unavailable',
        )
    return highlight_service


def _policies() -> RoomUploadPolicyManager:
    if policy_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Upload policies are unavailable',
        )
    return policy_manager


def _categories() -> UploadCategoryCatalog:
    if category_catalog is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Upload categories are unavailable',
        )
    return category_catalog


router = APIRouter(prefix='/browser-extension', tags=['browser-extension'])


@router.post('/pair', response_model=PairResponse, status_code=status.HTTP_201_CREATED)
async def pair(request: Request, command: PairRequest) -> PairResponse:
    store = security.auth_store
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='管理员认证尚未就绪'
        )
    client_key = request.client.host if request.client is not None else 'unknown'
    try:
        credentials = store.issue_extension_token(
            command.username, client_key=client_key
        )
    except AuthenticationRateLimited as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='配对尝试过多，请稍后再试',
            headers={'Retry-After': str(error.retry_after)},
        ) from None
    except AuthenticationFailed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail='管理员用户名不正确'
        ) from None
    return PairResponse(token_id=credentials.token_id, token=credentials.token)


@router.get('/rooms/{room_id}', response_model=RoomStatusResponse)
async def room_status(
    room_id: int,
    _identity: ExtensionIdentity = Depends(security.authenticated_extension),
    app: Application = Depends(_application),
) -> RoomStatusResponse:
    if room_id <= 0 or not app.has_task(room_id):
        return RoomStatusResponse(collected=False, recording=False)
    try:
        recording = (
            app.get_task_data(room_id).task_status.running_status
            == RunningStatus.RECORDING
        )
    except Exception:
        recording = False
    return RoomStatusResponse(collected=True, recording=recording)


@router.post(
    '/rooms/{room_id}/collect',
    response_model=CollectResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def collect_room(
    room_id: int,
    command: CollectRequest,
    identity: ExtensionIdentity = Depends(security.authenticated_extension),
    app: Application = Depends(_application),
) -> CollectResponse:
    if room_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='直播间编号无效'
        )
    try:
        operation = await app.submit_room_collect(room_id, upload=command.upload)
    except (ControlJournalClosed, ControlLaneSaturated) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='房间操作队列繁忙，请稍后重试',
            headers={'Retry-After': '1'},
        ) from error
    audit(
        'browser_extension_room_collect_submitted',
        token_id=identity.token_id,
        requested_room_id=room_id,
        upload=command.upload,
        operation_id=operation.id,
        result='accepted',
    )
    return CollectResponse(
        operation_id=operation.id, status=operation.status, requested_room_id=room_id
    )


@router.post(
    '/rooms/{room_id}/highlights',
    response_model=HighlightResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_highlight(
    room_id: int,
    command: HighlightRequest,
    identity: ExtensionIdentity = Depends(security.authenticated_extension),
    highlights: HighlightService = Depends(_highlights),
) -> HighlightResponse:
    marker = await highlights.create_marker(
        room_id=room_id,
        observed_at_ms=command.observed_at_ms,
        player_delay_ms=command.player_delay_ms,
        current_time_ms=command.current_time_ms,
        seekable_end_ms=command.seekable_end_ms,
        raw_delay_ms=command.raw_delay_ms,
        baseline_delay_ms=command.baseline_delay_ms,
        effective_rewind_ms=command.effective_rewind_ms,
        title=command.title,
        anchor_name=command.anchor_name,
        name=command.name,
        source='browser_extension',
    )
    audit(
        'browser_extension_highlight_created',
        token_id=identity.token_id,
        room_id=room_id,
        marker_id=marker.id,
        player_delay_ms=command.player_delay_ms,
        raw_delay_ms=command.raw_delay_ms,
        baseline_delay_ms=command.baseline_delay_ms,
        effective_rewind_ms=command.effective_rewind_ms,
        result='created',
    )
    return HighlightResponse(id=marker.id, name=marker.name)


async def _enable_upload_policy(
    room_id: int, policies: RoomUploadPolicyManager, catalog: UploadCategoryCatalog
) -> None:
    try:
        current = await policies.get(room_id)
    except RoomUploadPolicyNotFound:
        command = default_room_upload_policy()
    else:
        if current.enabled and not current.blocked_reason:
            return
        command = RoomUploadPolicyCommand(
            **{
                field.name: (
                    True if field.name == 'enabled' else getattr(current, field.name)
                )
                for field in fields(RoomUploadPolicyCommand)
            }
        )
    try:
        category_view = await catalog.list(command.account_mode, command.account_id)
        if not any(
            child.id == command.tid
            for parent in category_view.categories
            for child in parent.children
        ):
            raise InvalidRoomUploadPolicy('默认投稿分区当前不可用')
        if not any(
            statement.id == command.creation_statement_id
            for statement in category_view.creation_statements
        ):
            raise InvalidRoomUploadPolicy('默认创作声明当前不可用')
        result = await policies.upsert(room_id, command)
        if result.blocked_reason:
            raise InvalidRoomUploadPolicy(result.blocked_reason)
    except (
        InvalidRoomUploadPolicy,
        InvalidUploadCategoryRequest,
        UploadCategoryUnavailable,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='请先在 BLREC 中配置可用投稿账号和投稿分区：{}'.format(error),
        ) from None
