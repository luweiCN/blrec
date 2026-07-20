from fastapi import APIRouter, Body, HTTPException

from ...application import Application
from ...setting.file_work import SettingsFileWorkSaturated
from ..schemas import ResponseMessage

app: Application = None  # type: ignore  # bypass flake8 F821

router = APIRouter(prefix='/api/v1/validation', tags=['validation'])


@router.post('/dir', response_model=ResponseMessage)
async def validate_dir(path: str = Body(..., embed=True)) -> ResponseMessage:
    """Check if the path is a directory and grants the read, write permissions"""
    try:
        code, message = await app.validate_directory(path)
    except SettingsFileWorkSaturated as error:
        raise HTTPException(
            status_code=503,
            detail='settings file work is saturated',
            headers={'Retry-After': str(error.retry_after)},
        ) from error
    return ResponseMessage(code=code, message=message)


@router.post('/cookie', response_model=ResponseMessage)
async def validate_cookie(cookie: str = Body(..., embed=True)) -> ResponseMessage:
    """Check if the cookie is valid"""
    json_res = await app.validate_bili_cookie(cookie)
    return ResponseMessage(
        code=json_res['code'], message=json_res['message'], data=json_res['data']
    )
