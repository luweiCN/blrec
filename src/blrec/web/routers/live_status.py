from fastapi import APIRouter, Depends, status

from ...application import Application, CoordinatorMetrics
from .. import security

app: Application = None  # type: ignore


def get_application() -> Application:
    return app


router = APIRouter(prefix='/live-status', tags=['live-status'])


@router.get('')
async def get_live_status(
    application: Application = Depends(get_application),
) -> CoordinatorMetrics:
    return application.get_live_status_metrics()


@router.post(
    '/resume',
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(security.authenticate)],
)
async def resume_live_status(
    application: Application = Depends(get_application),
) -> None:
    application.resume_live_status_coordinator()
