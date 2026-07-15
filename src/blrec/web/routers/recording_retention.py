from typing import Optional

from fastapi import APIRouter, Depends, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel

from blrec.bili_upload.retention import RetentionManager, RetentionStatus
from blrec.utils.string import camel_case

from .bili_accounts import authenticated_manager_subject

manager: Optional[RetentionManager] = None
unavailable_reason: Optional[str] = 'Recording retention is not ready'


class RetentionStatusResponse(BaseModel):
    managed_video_bytes: int
    capacity_bytes: int
    remaining_bytes: int
    warning_threshold_bytes: int
    warning: bool

    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


def get_manager() -> RetentionManager:
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Recording retention is unavailable',
        )
    return manager


router = APIRouter(prefix='/recording-retention', tags=['recording-retention'])


@router.get('/status', response_model=RetentionStatusResponse)
async def get_retention_status(
    _subject: str = Depends(authenticated_manager_subject),
    retention_manager: RetentionManager = Depends(get_manager),
) -> RetentionStatus:
    return await retention_manager.status()
