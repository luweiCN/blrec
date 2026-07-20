from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response

from ...application import Application
from ...setting import SettingsIn, SettingsOut, TaskOptions
from ...setting.file_work import SettingsFileWorkSaturated
from ...setting.typing import KeySetOfSettings
from ..dependencies import settings_exclude_set, settings_include_set
from ..responses import not_found_responses

app: Application = None  # type: ignore  # bypass flake8 F821

router = APIRouter(prefix='/api/v1/settings', tags=['settings'])


@router.get('', response_model=SettingsOut, response_model_exclude_unset=True)
async def get_settings(
    include: Optional[KeySetOfSettings] = Depends(settings_include_set),
    exclude: Optional[KeySetOfSettings] = Depends(settings_exclude_set),
) -> SettingsOut:
    return app.get_settings(include, exclude)


@router.patch('', response_model=SettingsOut, response_model_exclude_unset=True)
async def change_settings(settings: SettingsIn, response: Response) -> SettingsOut:
    """Change settings of the application

    Change network request headers will cause
    **all** the Danmaku client be **reconnected**!
    """
    try:
        result, operation_ids = await app.change_settings_with_operations(settings)
    except SettingsFileWorkSaturated as error:
        raise HTTPException(
            status_code=503,
            detail='settings file work is saturated',
            headers={'Retry-After': str(error.retry_after)},
        ) from error
    if operation_ids:
        response.headers['X-BLREC-Operation-ID'] = operation_ids[0]
    return result


@router.get(
    '/tasks/{room_id}',
    response_model=TaskOptions,
    response_model_exclude_unset=True,
    responses={**not_found_responses},
)
async def get_task_options(room_id: int) -> TaskOptions:
    return app.get_task_options(room_id)


@router.patch(
    '/tasks/{room_id}', response_model=TaskOptions, responses={**not_found_responses}
)
async def change_task_options(
    room_id: int, options: TaskOptions, response: Response
) -> TaskOptions:
    """Change task-specific options

    Task-specific options will shadow the corresponding global settings.
    Explicitly set options to **null** will remove the value shadowing.
    """
    try:
        result, operation_ids = await app.change_task_options_with_operations(
            room_id, options
        )
    except SettingsFileWorkSaturated as error:
        raise HTTPException(
            status_code=503,
            detail='settings file work is saturated',
            headers={'Retry-After': str(error.retry_after)},
        ) from error
    if operation_ids:
        response.headers['X-BLREC-Operation-ID'] = operation_ids[0]
    return result
