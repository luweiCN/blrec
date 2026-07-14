import hashlib
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel

from blrec.bili_upload.account_lifecycle import (
    AccountRelationships,
    AccountRemovalBlocked,
    AccountRemovalCommand,
    AccountRemovalResult,
    InvalidAccountReplacement,
    RemovalMode,
)
from blrec.bili_upload.accounts import (
    AccountManager,
    AccountNotFound,
    AccountPaused,
    AccountView,
    CredentialVersionChanged,
    QrSessionForbidden,
    QrSessionNotFound,
    QrSessionView,
)
from blrec.bili_upload.errors import DefinitelyNotSent, RemoteOutcomeUnknown
from blrec.utils.string import camel_case

from .. import security

manager: Optional[AccountManager] = None
unavailable_reason: Optional[str] = 'Bilibili account management is not enabled'


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class AccountResponse(ApiModel):
    id: int
    uid: int
    display_name: str
    avatar_url: str
    credential_version: int
    credential_expires_at: int
    created_at: int
    state: str
    is_primary: bool


class QrSessionResponse(ApiModel):
    id: str
    state: str
    qr_url: Optional[str]
    expires_at: int
    account_id: Optional[int]


class RefreshResponse(ApiModel):
    credential_version: int
    refreshed: bool


class RelatedUploadJobResponse(ApiModel):
    id: int
    room_id: int
    state: str


class AccountRelationshipsResponse(ApiModel):
    account_id: int
    is_primary: bool
    follow_primary_room_ids: List[int]
    fixed_room_ids: List[int]
    reassignable_jobs: List[RelatedUploadJobResponse]
    blocking_jobs: List[RelatedUploadJobResponse]
    historical_job_count: int


class AccountRemovalRequest(ApiModel):
    mode: RemovalMode
    replacement_account_id: Optional[int] = None
    new_primary_account_id: Optional[int] = None

    def to_command(self) -> AccountRemovalCommand:
        return AccountRemovalCommand(
            mode=self.mode,
            replacement_account_id=self.replacement_account_id,
            new_primary_account_id=self.new_primary_account_id,
        )


class AccountRemovalResponse(ApiModel):
    account_id: int
    state: str


def get_account_manager() -> AccountManager:
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Bilibili account management is unavailable',
        )
    return manager


async def authenticated_manager_subject(
    request: Request, x_api_key: Optional[str] = Header(None)
) -> str:
    if not security.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail='API key is not configured'
        )
    await security.authenticate(request, x_api_key)
    assert request.client is not None
    assert x_api_key is not None
    value = '{}\0{}'.format(request.client.host, x_api_key).encode('utf8')
    return hashlib.sha256(value).hexdigest()


router = APIRouter(prefix='/bili-accounts', tags=['bili-accounts'])


@router.get('', response_model=List[AccountResponse])
async def list_accounts(
    _subject: str = Depends(authenticated_manager_subject),
    account_manager: AccountManager = Depends(get_account_manager),
) -> List[AccountView]:
    return await account_manager.list_accounts()


@router.put('/{account_id}/primary', response_model=AccountResponse)
async def select_primary_account(
    account_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    account_manager: AccountManager = Depends(get_account_manager),
) -> AccountView:
    try:
        return await account_manager.set_primary_account(account_id)
    except AccountNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Bilibili account not found'
        ) from None
    except AccountPaused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Only an active Bilibili account can be selected',
        ) from None


@router.get('/{account_id}/relationships', response_model=AccountRelationshipsResponse)
async def account_relationships(
    account_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    account_manager: AccountManager = Depends(get_account_manager),
) -> AccountRelationships:
    try:
        return await account_manager.account_relationships(account_id)
    except AccountNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Bilibili account not found'
        ) from None


@router.post('/{account_id}/removal', response_model=AccountRemovalResponse)
async def remove_account(
    account_id: int,
    payload: AccountRemovalRequest,
    subject: str = Depends(authenticated_manager_subject),
    account_manager: AccountManager = Depends(get_account_manager),
) -> AccountRemovalResult:
    try:
        return await account_manager.remove_account(
            account_id, payload.to_command(), manager_subject=subject
        )
    except AccountNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Bilibili account not found'
        ) from None
    except AccountRemovalBlocked as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                'code': 'blocking_upload_jobs',
                'jobIds': [job.id for job in error.jobs],
            },
        ) from None
    except InvalidAccountReplacement as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None


@router.post(
    '/qr-sessions',
    response_model=QrSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_qr_session(
    subject: str = Depends(authenticated_manager_subject),
    account_manager: AccountManager = Depends(get_account_manager),
) -> QrSessionView:
    return await account_manager.create_qr(manager_subject=subject)


@router.get('/qr-sessions/{session_id}', response_model=QrSessionResponse)
async def get_qr_session(
    session_id: str,
    subject: str = Depends(authenticated_manager_subject),
    account_manager: AccountManager = Depends(get_account_manager),
) -> QrSessionView:
    try:
        return await account_manager.status(session_id, manager_subject=subject)
    except QrSessionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='QR session not found'
        ) from None
    except QrSessionForbidden:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail='QR session is unavailable'
        ) from None


@router.delete('/qr-sessions/{session_id}', response_model=QrSessionResponse)
async def cancel_qr_session(
    session_id: str,
    subject: str = Depends(authenticated_manager_subject),
    account_manager: AccountManager = Depends(get_account_manager),
) -> QrSessionView:
    try:
        return await account_manager.cancel(session_id, manager_subject=subject)
    except QrSessionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='QR session not found'
        ) from None
    except QrSessionForbidden:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail='QR session is unavailable'
        ) from None


@router.post('/{account_id}/refresh', response_model=RefreshResponse)
async def refresh_account(
    account_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    account_manager: AccountManager = Depends(get_account_manager),
) -> RefreshResponse:
    try:
        result = await account_manager.check_account_renewal(account_id)
    except AccountNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='Bilibili account not found'
        ) from None
    except (
        AccountPaused,
        CredentialVersionChanged,
        DefinitelyNotSent,
        RemoteOutcomeUnknown,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Bilibili account refresh requires operator recovery',
        ) from None
    return RefreshResponse(
        credential_version=result.credential_version, refreshed=result.refreshed
    )
