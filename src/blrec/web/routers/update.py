from fastapi import APIRouter
from loguru import logger

from ... import __prog__
from ...application import Application

app: Application = None  # type: ignore  # bypass flake8 F821

router = APIRouter(prefix='/api/v1/update', tags=['update'])


@router.get('/version/latest')
async def get_latest_version() -> str:
    try:
        return await app.get_latest_version_string(__prog__) or ''
    except Exception as error:
        logger.warning('Update metadata request failed: {}', type(error).__name__)
        return ''
