from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...control.operations import ControlOperationJournal, ControlOperationSnapshot
from ...utils.string import camel_case

journal: Optional[ControlOperationJournal] = None

router = APIRouter(prefix='/control-operations', tags=['control-operations'])


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class ControlStepResponse(ApiModel):
    key: str
    generation: int
    status: Literal['queued', 'rejected', 'running', 'succeeded', 'failed']
    result: Optional[Dict[str, Any]]
    error_code: Optional[str]


class ControlOperationResponse(ApiModel):
    id: str
    lane: str
    kind: str
    target_key: str
    attempt: int
    generation: int
    status: Literal['accepted', 'running', 'succeeded', 'failed']
    result: Optional[Dict[str, Any]]
    error_code: Optional[str]
    created_at: float
    updated_at: float
    steps: List[ControlStepResponse]


def operation_response(operation: ControlOperationSnapshot) -> ControlOperationResponse:
    return ControlOperationResponse(
        id=operation.id,
        lane=operation.lane,
        kind=operation.kind,
        target_key=operation.target_key,
        attempt=operation.attempt,
        generation=operation.generation,
        status=operation.status,
        result=None if operation.result is None else dict(operation.result),
        error_code=operation.error_code,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
        steps=[
            ControlStepResponse(
                key=step.key,
                generation=step.generation,
                status=step.status,
                result=None if step.result is None else dict(step.result),
                error_code=step.error_code,
            )
            for step in operation.steps
        ],
    )


@router.get('/{operation_id}', response_model=ControlOperationResponse)
async def get_control_operation(operation_id: str) -> ControlOperationResponse:
    if journal is None:
        raise HTTPException(status_code=503, detail='control journal unavailable')
    operation = await journal.get(operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail='control operation not found')
    return operation_response(operation)
