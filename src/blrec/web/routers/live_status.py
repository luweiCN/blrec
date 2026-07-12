from typing import Optional

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.exceptions import HTTPException

from ...application import Application, CoordinatorMetrics
from .. import security

app: Application = None  # type: ignore


def get_application() -> Application:
    return app


async def authenticate_resume(
    request: Request, x_api_key: Optional[str] = Header(None)
) -> None:
    if not security.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail='API key is not configured'
        )
    await security.authenticate(request, x_api_key)


router = APIRouter(prefix='/live-status', tags=['live-status'])


@router.get('')
async def get_live_status(
    application: Application = Depends(get_application),
) -> CoordinatorMetrics:
    return application.get_live_status_metrics()


@router.post(
    '/resume',
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(authenticate_resume)],
)
async def resume_live_status(
    application: Application = Depends(get_application),
) -> None:
    application.resume_live_status_coordinator()
