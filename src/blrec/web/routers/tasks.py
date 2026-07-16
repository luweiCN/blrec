from typing import Any, Dict, List, Literal

import attr
from fastapi import APIRouter, BackgroundTasks, Body, Depends, status
from pydantic import BaseModel, Field, PositiveInt, conint, validator

from ...application import Application
from ...exception import ForbiddenError, NotFoundError
from ...logging.audit import audit
from ...utils.ffprobe import StreamProfile
from ...utils.string import camel_case
from ..dependencies import TaskDataFilter, task_data_filter
from ..responses import (
    accepted_responses,
    confict_responses,
    created_responses,
    forbidden_responses,
    not_found_responses,
)
from ..schemas import ResponseMessage

app: Application = None  # type: ignore  # bypass flake8 F821

router = APIRouter(prefix='/api/v1/tasks', tags=['tasks'])


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class TaskBatchActionRequest(ApiModel):
    action: Literal[
        'start',
        'stop',
        'force_stop',
        'recorder_enable',
        'recorder_disable',
        'recorder_force_disable',
        'refresh',
        'cut',
        'delete',
    ]
    room_ids: List[int] = Field(..., min_items=1, max_items=100)

    @validator('room_ids')
    def room_ids_must_be_unique(cls, value: List[int]) -> List[int]:
        if any(room_id <= 0 for room_id in value):
            raise ValueError('room IDs must be positive')
        if len(set(value)) != len(value):
            raise ValueError('room IDs must be unique')
        return value


class TaskBatchActionResult(ApiModel):
    room_id: int
    accepted: bool
    message: str


class TaskBatchActionResponse(ApiModel):
    results: List[TaskBatchActionResult]


@router.get('/data')
async def get_task_data(
    page: PositiveInt = 1,
    size: conint(ge=10, le=100) = 100,  # type: ignore
    filter: TaskDataFilter = Depends(task_data_filter),
) -> List[Dict[str, Any]]:
    start = (page - 1) * size
    stop = page * size

    task_data = []
    for index, data in enumerate(filter(app.get_all_task_data())):
        if index < start:
            continue
        if index >= stop:
            break
        task_data.append(attr.asdict(data))

    return task_data


@router.post('/actions', response_model=TaskBatchActionResponse)
async def run_task_batch_action(
    command: TaskBatchActionRequest,
) -> TaskBatchActionResponse:
    results = []
    for room_id in command.room_ids:
        if not app.has_task(room_id):
            results.append(
                TaskBatchActionResult(
                    room_id=room_id, accepted=False, message='录制任务不存在'
                )
            )
            continue
        try:
            if command.action == 'start':
                await app.start_task(room_id)
                message = '任务已运行'
            elif command.action == 'stop':
                await app.stop_task(room_id, False)
                message = '任务已停止'
            elif command.action == 'force_stop':
                await app.stop_task(room_id, True)
                message = '任务已强制停止'
            elif command.action == 'recorder_enable':
                await app.enable_task_recorder(room_id)
                message = '录制已开启'
            elif command.action == 'recorder_disable':
                await app.disable_task_recorder(room_id, False)
                message = '录制已关闭'
            elif command.action == 'recorder_force_disable':
                await app.disable_task_recorder(room_id, True)
                message = '录制已强制关闭'
            elif command.action == 'refresh':
                await app.update_task_info(room_id)
                message = '任务数据已刷新'
            elif command.action == 'cut':
                if not app.cut_stream(room_id):
                    results.append(
                        TaskBatchActionResult(
                            room_id=room_id, accepted=False, message='当前不能切割文件'
                        )
                    )
                    continue
                message = '已触发文件切割'
            else:
                await app.remove_task(room_id)
                message = '任务已删除'
        except Exception as error:
            results.append(
                TaskBatchActionResult(
                    room_id=room_id, accepted=False, message=str(error) or '操作失败'
                )
            )
        else:
            results.append(
                TaskBatchActionResult(room_id=room_id, accepted=True, message=message)
            )
    rejected = sum(not result.accepted for result in results)
    audit(
        'recording_task_action',
        level='WARNING' if rejected else 'INFO',
        action=command.action,
        room_ids=command.room_ids,
        accepted=len(results) - rejected,
        rejected=rejected,
    )
    return TaskBatchActionResponse(results=results)


@router.get('/{room_id}/data', responses={**not_found_responses})
async def get_one_task_data(room_id: int) -> Dict[str, Any]:
    return attr.asdict(app.get_task_data(room_id))


@router.get('/{room_id}/param', responses={**not_found_responses})
async def get_task_param(room_id: int) -> Dict[str, Any]:
    return attr.asdict(app.get_task_param(room_id))


@router.get('/{room_id}/metadata', responses={**not_found_responses})
async def get_task_metadata(room_id: int) -> Dict[str, Any]:
    metadata = app.get_task_metadata(room_id)
    if not metadata:
        return {}
    return attr.asdict(metadata)


@router.get('/{room_id}/profile', responses={**not_found_responses})
async def get_task_stream_profile(room_id: int) -> StreamProfile:
    return app.get_task_stream_profile(room_id)


@router.get('/{room_id}/videos', responses={**not_found_responses})
async def get_task_video_file_details(room_id: int) -> List[Dict[str, Any]]:
    return [attr.asdict(d) for d in app.get_task_video_file_details(room_id)]


@router.get('/{room_id}/danmakus', responses={**not_found_responses})
async def get_task_danmaku_file_details(room_id: int) -> List[Dict[str, Any]]:
    return [attr.asdict(d) for d in app.get_task_danmaku_file_details(room_id)]


@router.post('/info', response_model=ResponseMessage, responses={**not_found_responses})
async def update_all_task_infos() -> ResponseMessage:
    await app.update_all_task_infos()
    return ResponseMessage(message='All task infos have been updated')


@router.post(
    '/{room_id}/info', response_model=ResponseMessage, responses={**not_found_responses}
)
async def update_task_info(room_id: int) -> ResponseMessage:
    await app.update_task_info(room_id)
    return ResponseMessage(message='The task info has been updated')


@router.get(
    '/{room_id}/cut', response_model=ResponseMessage, responses={**not_found_responses}
)
async def can_cut_stream(room_id: int) -> ResponseMessage:
    if app.can_cut_stream(room_id):
        return ResponseMessage(message='The stream can been cut', data={'result': True})
    else:
        return ResponseMessage(
            message='The stream cannot been cut', data={'result': False}
        )


@router.post(
    '/{room_id}/cut',
    response_model=ResponseMessage,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**not_found_responses, **forbidden_responses},
)
async def cut_stream(room_id: int) -> ResponseMessage:
    if not app.cut_stream(room_id):
        raise ForbiddenError('The stream cannot been cut')
    return ResponseMessage(message='The stream cutting have been triggered')


@router.post(
    '/start', response_model=ResponseMessage, responses={**not_found_responses}
)
async def start_all_tasks() -> ResponseMessage:
    await app.start_all_tasks()
    return ResponseMessage(message='All tasks have been started')


@router.post(
    '/{room_id}/start',
    response_model=ResponseMessage,
    responses={**not_found_responses},
)
async def start_task(room_id: int) -> ResponseMessage:
    await app.start_task(room_id)
    return ResponseMessage(message='The task has been started')


@router.post(
    '/stop',
    response_model=ResponseMessage,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**accepted_responses},
)
async def stop_all_tasks(
    background_tasks: BackgroundTasks,
    force: bool = Body(False),
    background: bool = Body(False),
) -> ResponseMessage:
    if background:
        background_tasks.add_task(app.stop_all_tasks, force)
        return ResponseMessage(message='Stopping all tasks on the background')

    await app.stop_all_tasks(force)
    return ResponseMessage(message='All tasks have been stopped')


@router.post(
    '/{room_id}/stop',
    response_model=ResponseMessage,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**not_found_responses, **accepted_responses},
)
async def stop_task(
    background_tasks: BackgroundTasks,
    room_id: int,
    force: bool = Body(False),
    background: bool = Body(False),
) -> ResponseMessage:
    if not app.has_task(room_id):
        raise NotFoundError(f'No task for the room {room_id}')

    if background:
        background_tasks.add_task(app.stop_task, room_id, force)
        return ResponseMessage(message='Stopping the task on the background')

    await app.stop_task(room_id, force)
    return ResponseMessage(message='The task has been stopped')


@router.post('/recorder/enable', response_model=ResponseMessage)
async def enable_all_task_recorders() -> ResponseMessage:
    await app.enable_all_task_recorders()
    return ResponseMessage(message='All task recorders have been enabled')


@router.post(
    '/{room_id}/recorder/enable',
    response_model=ResponseMessage,
    responses={**not_found_responses},
)
async def enable_task_recorder(room_id: int) -> ResponseMessage:
    await app.enable_task_recorder(room_id)
    return ResponseMessage(message='The task recorder has been enabled')


@router.post(
    '/recorder/disable',
    response_model=ResponseMessage,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**accepted_responses},
)
async def disable_all_task_recorders(
    background_tasks: BackgroundTasks,
    force: bool = Body(False),
    background: bool = Body(False),
) -> ResponseMessage:
    if background:
        background_tasks.add_task(app.disable_all_task_recorders, force)
        return ResponseMessage(message='Disabling all task recorders on the background')

    await app.disable_all_task_recorders(force)
    return ResponseMessage(message='All task recorders have been disabled')


@router.post(
    '/{room_id}/recorder/disable',
    response_model=ResponseMessage,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**not_found_responses, **accepted_responses},
)
async def disable_task_recorder(
    background_tasks: BackgroundTasks,
    room_id: int,
    force: bool = Body(False),
    background: bool = Body(False),
) -> ResponseMessage:
    if not app.has_task(room_id):
        raise NotFoundError(f'No task for the room {room_id}')

    if background:
        background_tasks.add_task(app.disable_task_recorder, room_id, force)
        return ResponseMessage(message='Disabling the task recorder on the background')

    await app.disable_task_recorder(room_id, force)
    return ResponseMessage(message='The task recorder has been disabled')


@router.post(
    '/{room_id}',
    response_model=ResponseMessage,
    status_code=status.HTTP_201_CREATED,
    responses={**created_responses, **confict_responses, **forbidden_responses},
)
async def add_task(room_id: int) -> ResponseMessage:
    """Add a task for a room.

    the room_id argument can be both the real room id or the short room id.

    only use the real room id for task management.
    the short room id should be considered as a shorthand
    only for adding tasks conveniently.
    """
    real_room_id = await app.add_task(room_id)
    return ResponseMessage(
        message='Successfully Added Task', data={'room_id': real_room_id}
    )


@router.delete('', response_model=ResponseMessage)
async def remove_all_tasks() -> ResponseMessage:
    await app.remove_all_tasks()
    return ResponseMessage(message='All tasks have been removed')


@router.delete(
    '/{room_id}', response_model=ResponseMessage, responses={**not_found_responses}
)
async def remove_task(room_id: int) -> ResponseMessage:
    await app.remove_task(room_id)
    return ResponseMessage(message='The task has been removed')
